# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities for generating text."""

import os
import pickle
import re
from collections.abc import Iterable
from functools import partial
from typing import Callable, Tuple

import numpy as np
import torch
import torch.nn.functional as F

from nemo.collections.common.tokenizers.tabular_tokenizer import TabularTokenizer
from nemo.collections.nlp.modules.common.megatron.utils import get_ltor_masks_and_position_ids
from nemo.collections.nlp.modules.common.text_generation_strategy import model_inference_strategy_dispatcher
from nemo.collections.nlp.modules.common.transformer.text_generation import LengthParam, OutputType, SamplingParam
from nemo.utils import AppState
from nemo_aligner.utils.deep_search.communication_util import receive_generate_info, send_generate_info, get_model_parallel_src_rank

try:
    from apex.transformer.pipeline_parallel.utils import _reconfigure_microbatch_calculator

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):

    HAVE_APEX = False

try:
    from megatron.core import parallel_state, tensor_parallel

    HAVE_MEGATRON_CORE = True

except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False

__all__ = [
    "megatron_gpt_generate",
    "megatron_neva_generate",
    "generate",
]


def top_k_logits(logits, top_k=0, top_p=0.0, filter_value=-float("Inf"), started=None):
    """
       This function has been mostly taken from huggingface conversational
         ai code at
         https://medium.com/huggingface/how-to-build-a-state-of-the-art-
              conversational-ai-with-transfer-learning-2d818ac26313 

        @param logits: logits tensor
        @param top_k: keep only top k tokens with highest probability
        @param top_p: keep the top tokens with cumulative probability
        @filter_value: value to set filtered tokens to
        @started: a tensor of bools indicating whether the text generation starts for the batch
        returns the filtered logits
    """
    if top_k > 0:
        # Remove all tokens with a probability less than the
        # last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        if started is not None:
            for i in np.arange(indices_to_remove.size(0))[started.cpu().numpy()]:
                logits[i, indices_to_remove[i]] = filter_value
        else:
            logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        # Cconvert to 1D
        sorted_logits, sorted_indices = torch.sort(logits, descending=True, dim=-1)
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token
        # above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0
        if started is not None:
            for i in np.arange(sorted_indices.size(0))[started.cpu().numpy()]:
                indices_to_remove = sorted_indices[i][sorted_indices_to_remove[i]]
                logits[i, indices_to_remove] = filter_value
        else:
            for i in range(sorted_indices.size(0)):
                indices_to_remove = sorted_indices[i][sorted_indices_to_remove[i]]
                logits[i, indices_to_remove] = filter_value

    return logits




def search(
    model,
    inputs=None,
    action=None,
    depth=None,
    sessions=None,
    tokens_to_generate=1,  # max search depth
    top_k=0,
    end_strings=["<|endoftext|>"],
    **strategy_args,
) -> OutputType:
    """
    """
    if "strategy" in strategy_args:
        inference_strategy = strategy_args["strategy"]
    else:
        raise ValueError("strategy is not specified")
    tokenizer = model.tokenizer
    if torch.distributed.get_rank() == get_model_parallel_src_rank():
        if inputs is None:
            # not the first node
            assert action is not None
            assert depth is not None
            # action is a tensor of shape [batch_size, 1], type int32
            # it is the selected actions during tree search from node at depth, type int32
            # depth is a tensor of shape [batch_size, 1]
            # from the action and depth values, we can retrieve the node
            action = torch.cuda.IntTensor(action)
            depth = torch.cuda.IntTensor(depth)
            batch_size = action.shape[0]
            context_tokens_tensor = torch.cuda.LongTensor(batch_size, 1)
            context_length_tensor = torch.cuda.LongTensor(batch_size)

        else:
            # first time to run inference,
            # need to initialize the root node, kv-cache
            assert action is None
            assert depth is None
            context_tokens_tensor, context_length_tensor = inference_strategy.tokenize_batch(
                inputs, 0, False
            )
            batch_size = context_tokens_tensor.size(0)
            depth = torch.cuda.IntTensor(batch_size, 1)
            depth[:] = 0
            action = torch.cuda.IntTensor(batch_size, 1)
            action[:] = 0

        send_generate_info(
            context_tokens_tensor,
            context_length_tensor,
            action,
            depth,
            tokens_to_generate,
            top_k,
            end_strings,
            sessions,
        )
    else:
        (
            context_tokens_tensor,
            context_length_tensor,
            action,
            depth,
            tokens_to_generate,
            top_k,
            end_strings,
            sessions,
        ) = receive_generate_info()
    
    # init the objects for inference
    init = depth[0].item() == 0
    if init:
        inference_strategy.init(context_tokens_tensor, tokens_to_generate, sessions)
    else:
        context_tokens_tensor, context_length_tensor = inference_strategy.compute_inference_params(sessions, depth, action)

    output_actions, output_policys = sample_sequence_batch(
        model,
        inference_strategy,
        context_tokens_tensor,
        context_length_tensor,
        tokens_to_generate,
        end_strings=end_strings,
        sessions=sessions,
        top_k=top_k,
        init=init,
        depths=depth,
    )
    output = {}
    output["action"] = output_actions
    output["policy"] = output_policys
    output = inference_strategy.post_generation_process(output)
    return output


def switch(val1, val2, boolean):
    boolean = boolean.type_as(val1)
    return (1 - boolean) * val1 + boolean * val2


def sample_sequence_batch(
    model,
    inference_strategy,
    context_tokens,
    context_lengths,
    tokens_to_generate,
    end_strings,
    sessions,
    top_k,
    init,
    depths):
    # Importing here to avoid circular import errors

    app_state = AppState()
    micro_batch_size = context_tokens.shape[0]
    _reconfigure_microbatch_calculator(
        rank=app_state.global_rank,
        rampup_batch_size=None,
        global_batch_size=micro_batch_size,
        micro_batch_size=micro_batch_size,
        data_parallel_size=1,
    )
    assert (
        model.cfg.get("sequence_parallel", False) == False
    ), "sequence_parallel should be False during inference. Disable it in the model config if restoring from nemo or in hparams.yaml if restoring from PTL checkpoint"
    assert (
        model.cfg.get("activations_checkpoint_granularity", None) is None
    ), "activations_checkpoint_granularity should be None during inference. Disable it in the model config if restoring from nemo or in hparams.yaml if restoring from PTL checkpoint"
    assert (
        model.cfg.get("activations_checkpoint_method", None) is None
    ), "activations_checkpoint_method should be None during inference. Disable it in the model config if restoring from nemo or in hparams.yaml if restoring from PTL checkpoint"

    tokenizer = model.tokenizer
    # initialize the batch
    with torch.no_grad():
        # get min context length
        context_length = context_lengths.min().item()

        counter = 0

        batch_size = context_tokens.size(0)

        tokens = context_tokens

        maxlen = 1 + context_lengths.max().item()

        maxlen = inference_strategy.clip_max_len(maxlen)


        output_actions = torch.cuda.IntTensor(batch_size, top_k)
        output_policy = torch.cuda.FloatTensor(batch_size, top_k)

        while context_length < maxlen:
            batch, tensor_shape = inference_strategy.prepare_batch_at_step(
                tokens, micro_batch_size, context_length, init, sessions, counter,
            )

            if init:
                output = inference_strategy.forward_step(batch, tensor_shape, sessions, init)
            else:
                output = None
            if parallel_state.is_pipeline_last_stage():

                logits = output[0]["logits"][:, -1].contiguous() # output[0]["logits"] shape[batch_size, length, partial vocab_size]
                logits = tensor_parallel.gather_from_tensor_model_parallel_region(logits)
                assert logits is not None
                logits = logits.view(batch_size, -1)

                # make sure it won't sample outside the vocab_size range
                logits[:, tokenizer.vocab_size :] = -float("Inf")

                updated_logits, actions = torch.topk(logits, top_k)
                probs = F.softmax(updated_logits, dim=-1)

                batch_update_indicator = context_lengths == context_length
                output_actions[batch_update_indicator] = actions[batch_update_indicator].type(torch.int32)
                output_policy[batch_update_indicator] = probs[batch_update_indicator]
 
            context_length += 1
            counter += 1
        # after inference, save the kv cache to the search db
        # inference_strategy.save_kv_cache(session_id)
        if init:
            parent_nodes = [None] * batch_size
            actions_taken = torch.cuda.IntTensor([-1] * batch_size)
            # construct and save the root node
            inference_strategy.save_kv_cache(sessions, depths, batch_size, context_lengths, parent_nodes, actions_taken, context_tokens)

            # construct and save the first level nodes
            depths += 1

            for i in range(batch_size):
                session_id = sessions[i]
                parent_nodes[i] = inference_strategy.get_node(session_id, 0, -1)

            for j in range(top_k):
                actions_taken = output_actions[:, j]
                inference_strategy.save_kv_cache(sessions, depths, batch_size, context_lengths, parent_nodes, actions_taken, None)
        
        # sync from last pipeline stage to src rank, so that it can be returned
        if parallel_state.is_pipeline_last_stage():
            src = parallel_state.get_pipeline_model_parallel_last_rank()
            group = parallel_state.get_embedding_group()
            torch.distributed.broadcast(output_actions, src, group)
            torch.distributed.broadcast(output_policy, src, group)
        elif parallel_state.is_pipeline_first_stage():
            src = parallel_state.get_pipeline_model_parallel_last_rank()
            group = parallel_state.get_embedding_group()
            torch.distributed.broadcast(output_actions, src, group)
            torch.distributed.broadcast(output_policy, src, group)
        return output_actions, output_policy


            # # construct and save the first level nodes
            # depths += 1

            # for i in range(batch_size):
            #     session_id = sessions[i]
            #     parent_nodes[i] = inference_strategy.get_node(session_id, 0, -1)

            # for j in range(top_k):
            #     actions_taken = output_actions[:, j]
            #     inference_strategy.save_kv_cache(sessions, depths, batch_size, context_lengths, parent_nodes, actions_taken)
          

        # if parallel_state.is_pipeline_last_stage():

        #     # if compute_logprob:
        #     #     output = output[0]["logits"]
        #     #     output = tensor_parallel.gather_from_tensor_model_parallel_region(output)
        #     #     assert output is not None
        #     #     logits = output[:, -1].view(batch_size, -1).contiguous()

        #     # else:
        #     #     logits = output[0]["logits"][:, -1].contiguous()
        #     #     logits = tensor_parallel.gather_from_tensor_model_parallel_region(logits)
        #     #     assert logits is not None
        #     #     logits = logits.view(batch_size, -1)
        #     logits = output[0]["logits"][:, -1].contiguous()
        #     logits = tensor_parallel.gather_from_tensor_model_parallel_region(logits)
        #     assert logits is not None
        #     logits = logits.view(batch_size, -1)


        #     # # make sure it will generate at least min_length
        #     # min_length = extra.get("min_tokens_to_generate", 0)
        #     # if min_length > 0:
        #     #     within_min_length = (context_length - context_lengths) < min_length
        #     #     logits[within_min_length, eod_id] = -float("Inf")

        #     # make sure it won't sample outside the vocab_size range
        #     logits[:, tokenizer.vocab_size :] = -float("Inf")

        #     # started indicates whether the current token step passes the context_length, so we make sure not to overwrite the context tokens

        #     # started = context_lengths <= context_length
        #     # if extra.get("greedy", False):
        #     #     prev = torch.argmax(logits, dim=-1).view(-1)
        #     # else:
        #     #     logits = logits.float()
        #     #     logits /= temperature
        #     #     # handle repetition penality
        #     #     logits = repetition_penalty(logits, extra.get("repetition_penalty", 1.2), all_generated_indices)
        #     #     logits = top_k_logits(
        #     #         logits, top_k=extra.get("top_k", 0), top_p=extra.get("top_p", 0.9), started=started
        #     #     )
        #     #     probs = F.softmax(logits, dim=-1)
        #     #     prev = torch.multinomial(probs, num_samples=1).view(-1)
        #     updated_logits, actions = torch.topk(logits, top_k)
        #     probs = F.softmax(updated_logits, dim=-1)
        #     # logits = logits.float()
        #     # # logits /= temperature
        #     # # # handle repetition penality
        #     # # logits = repetition_penalty(logits, extra.get("repetition_penalty", 1.2), all_generated_indices)
        #     # logits = top_k_logits(
        #     #     logits, top_k=extra.get("top_k", 0), top_p=extra.get("top_p", 0.9), started=started
        #     # )
        #     # probs = F.softmax(logits, dim=-1)
        #     # prev = torch.multinomial(probs, num_samples=1).view(-1)

        #     # Clamp the predicted out of vocabulary tokens
        #     # prev = torch.clamp(prev, max=tokenizer.vocab_size - 1)

        #     # new_tokens = switch(tokens[:, context_length].view(-1), prev, started)

        #     # Replace sampled tokens w/ done token if EOD has already been sampled
        #     # new_tokens = switch(new_tokens, eod_id, is_done)

        #     # post process the inference tokens based on the strategy
        #     # inference_strategy.post_process(tokens, new_tokens, context_length)

        #     # Insert either new predicted or next prompt token
        #     # tokens[:, context_length] = new_tokens

        #     src = parallel_state.get_pipeline_model_parallel_last_rank()
        #     group = parallel_state.get_embedding_group()
        #     torch.distributed.broadcast(actions, src, group)

        #     #                done_token = (prev == eod_id).byte() & started.byte()
        #     done_token = inference_strategy.end_of_generation_condition(
        #         tokens[:, : context_length + 1], prev, eod_id, end_strings
        #     )
        #     done_token = done_token.byte()

        #     just_finished = (done_token & ~is_done).bool()

        #     lengths[just_finished.view(-1)] = context_length

        #     is_done = is_done | done_token

        #     done = torch.all(is_done)
        #     src = parallel_state.get_pipeline_model_parallel_last_rank()
        #     group = parallel_state.get_pipeline_model_parallel_group()
        #     torch.distributed.broadcast(done, src, group)
        #     if compute_logprob:
        #         if all_probs:
        #             yield tokens, lengths, output_logits, full_logits
        #         else:
        #             yield tokens, lengths, output_logits, None
        #     else:
        #         yield tokens, lengths, None, None

        # else:
        #     if parallel_state.is_pipeline_first_stage():
        #         src = parallel_state.get_pipeline_model_parallel_last_rank()
        #         group = parallel_state.get_embedding_group()
        #         actions = torch.empty_like(logits[:, :top_k])
        #         new_tokens = torch.empty_like(tokens[:, context_length])
        #         torch.distributed.broadcast(new_tokens, src, group)
        #         tokens[:, context_length] = new_tokens
        #         yield tokens, None, None, None
        #     else:
        #         yield None, None, None, None

        #     done = torch.cuda.ByteTensor([0])
        #     src = parallel_state.get_pipeline_model_parallel_last_rank()
        #     group = parallel_state.get_pipeline_model_parallel_group()
        #     torch.distributed.broadcast(done, src, group)


def synced_generate(
    model,
    inference_strategy,
    context_tokens_tensor,
    context_length_tensor,
    tokens_to_generate, # max search depth
    top_k,
    end_strings,
    session_id,
    init,
):
    context_length = context_length_tensor.min().item()

    output = sample_sequence_batch(
        model,
        inference_strategy,
        context_tokens_tensor,
        context_length_tensor,
        tokens_to_generate,
        end_strings=end_strings,
        session_id=session_id,
        top_k=top_k,
        init=init,
    )

    # for tokens, lengths, output_logits, full_logits in batch_token_iterator:
    #     context_length += 1

    # if parallel_state.is_pipeline_last_stage():
    #     src = parallel_state.get_pipeline_model_parallel_last_rank()
    #     group = parallel_state.get_embedding_group()
    #     if compute_logprob:
    #         torch.distributed.broadcast(output_logits, src, group)
    #     if all_probs:
    #         src = parallel_state.get_pipeline_model_parallel_last_rank()
    #         group = parallel_state.get_embedding_group()
    #         torch.distributed.broadcast(full_logits, src, group)

    # else:
    #     if parallel_state.is_pipeline_first_stage():
    #         src = parallel_state.get_pipeline_model_parallel_last_rank()
    #         group = parallel_state.get_embedding_group()

    #         if compute_logprob:
    #             precision = model._trainer.precision
    #             dtype = torch.float32

    #             output_logits = torch.empty(
    #                 tokens.size(0), context_length - 1, dtype=dtype, device=torch.device("cuda")
    #             )
    #             torch.distributed.broadcast(output_logits, src, group)

    #         if all_probs:
    #             src = parallel_state.get_pipeline_model_parallel_last_rank()
    #             group = parallel_state.get_embedding_group()
    #             full_logits = torch.empty(
    #                 tokens.size(0),
    #                 context_length - 1,
    #                 model.padded_vocab_size,
    #                 dtype=dtype,
    #                 device=torch.device("cuda"),
    #             )
    #             torch.distributed.broadcast(full_logits, src, group)
    # if tokens is not None:
    #     return tokens[:, :context_length], output_logits, full_logits

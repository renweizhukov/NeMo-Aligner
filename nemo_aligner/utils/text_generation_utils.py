# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
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

from typing import Any, List

import torch

from megatron.core import parallel_state
from nemo.collections.nlp.modules.common.text_generation_strategy import GPTModelTextGenerationStrategy, TextGenerationStrategy
from nemo.utils import logging

from nemo_aligner.utils.distributed import broadcast_2d_tensor_within_pp


class TrackLengthGPTModelTextGenerationStrategy(GPTModelTextGenerationStrategy):
    """
    Text generation strategy that tracks the length of the generated text.

    TODO This is a temporary workaround until NeMo's `generate()` function returns this information.
    """

    def __init__(self, model: Any, context_lengths: torch.Tensor, max_length: int):
        super().__init__(model)
        self._context_lengths = context_lengths
        self._max_length = max_length
        self._end_idx = torch.full_like(context_lengths, fill_value=-1)

    def end_of_generation_condition(
        self, tokens: torch.Tensor, prev: torch.Tensor, eod_id: int, end_strings: List[str]
    ) -> torch.Tensor:
        is_end = super().end_of_generation_condition(tokens=tokens, prev=prev, eod_id=eod_id, end_strings=end_strings)
        assert len(is_end) == len(tokens)
        if len(tokens) != len(self._context_lengths):
            raise RuntimeError(
                "Batch size mismatch: the `context_lengths` tensor provided in the constructor has batch size "
                f"{len(self._context_lengths)}, while the generated tokens have batch size {len(tokens)}"
            )
        context_length = tokens.size(1) - 1  # the input tokens come from `tokens[:, : context_length + 1]`
        started = self._context_lengths <= context_length
        # The generation ends right now when three conditions hold:
        #   - it has started
        #   - the end generation is triggered now
        #   - it did *not* end before
        self._end_idx = torch.where(started & is_end & (self._end_idx < 0), context_length, self._end_idx)
        return is_end

    def get_lengths(self) -> torch.Tensor:
        """
        Return the total lengths of the generated sequences, in # of tokens.

        The total length of a generated sequence counts both:
            * the context tokens (i.e., the input prompt)
            * the token(s) that ended generation, if any (e.g. the `EOS` token or the token(s) corresponding to
              an element of `sampling_params.end_strings`)
        """
        lengths = None
        if parallel_state.is_pipeline_last_stage():  # only the last stage actually has access to lengths
            lengths = torch.where(self._end_idx >= 0, self._end_idx + 1, self._context_lengths + self._max_length)
            lengths = lengths.to(torch.int64).view((-1, 1))
        lengths = broadcast_2d_tensor_within_pp(lengths, dtype=torch.int64)
        return lengths.flatten()


def pad_batch(batch, pad_id):
    """batch each element of the batch to be the size of the longest sequence
    """
    context_lengths = []
    max_context_length = max([len(tokens) for tokens in batch])
    for tokens in batch:
        context_length = len(tokens)
        if context_length < max_context_length:
            tokens.extend([pad_id] * (max_context_length - context_length))
        context_lengths.append(context_length)
    return batch, context_lengths


def tokenize_batch(tokenizer, sentences, max_len, add_BOS, add_EOS=False):
    """convert the sentences into lists of tokens, pad them to the same length, add bos tokens if it is needed
    Args:
        sentences (List[str]): list of input sentences in str format.
        max_len (int): max number of tokens to generate.
        add_BOS (bool): whether to add the BOS token at the beginning
    Returns:
        Tuple[torch.Tensor], the tokenized and padded torch tensor and the token context length tensor.
    """

    def tokenize(sentence):
        output = tokenizer.text_to_ids(sentence)

        if add_BOS:
            output = [tokenizer.bos_id] + output

        if add_EOS:
            output.append(tokenizer.eos_id)

        return output

    context_tokens = list(map(tokenize, sentences))

    exceeded = [False] * len(context_tokens)

    for i, x in enumerate(context_tokens):
        if len(x) > max_len:
            logging.warning(f"max seq len of {max_len} exceeded, chunking")
            exceeded[i] = True

    context_tokens = [x[:max_len] for x in context_tokens]
    context_tokens, context_lengths = pad_batch(context_tokens, tokenizer.eos_id)
    context_tokens_tensor = torch.cuda.LongTensor(context_tokens)
    context_length_tensor = torch.cuda.LongTensor(context_lengths)
    return context_tokens_tensor, context_length_tensor, exceeded

class MGPTModelTextGenerationStrategy(TextGenerationStrategy):
    def __init__(self, model):
        super().__init__(model)
        self.forward_model = self.model.model
        self.tokenizer = self.model.tokenizer
        self.image_paths = []
        self.cfg = self.model.cfg
        self.data_cfg = self.model.cfg.data

        add_extra_token = 0
        self.image_token = self.cfg.mm_cfg.get("image_token", "<image>")
        self.video_token = self.cfg.mm_cfg.get("video_token", "<video>")
        self.image_patch_token = self.cfg.mm_cfg.get("image_patch_token", "<extra_id_3>")
        self.im_start_token = self.cfg.mm_cfg.get("im_start_token", "<extra_id_4>")
        self.im_end_token = self.cfg.mm_cfg.get("im_end_token", "<extra_id_5>")

        self.multimodal_cfg = dict(
            is_multimodal=self.data_cfg.is_multimodal,
            sep_image_conv_front=False,
            conv_template=None,
            patch_dim=self.cfg.mm_cfg.vision_encoder.patch_dim,
            crop_size=self.cfg.mm_cfg.vision_encoder.get("crop_size", None),
            image_folder=self.data_cfg.get('image_folder', None),
            video_folder=self.data_cfg.get('video_folder', None),
            image_aspect_ratio=self.data_cfg.image_aspect_ratio,
            use_im_start_end=getattr(self.cfg.mm_cfg, 'use_im_start_end', False),
            image_processor=None,
            add_extra_token=add_extra_token,
            context_length=self.cfg.encoder_seq_length,
            media_type=getattr(self.data_cfg, 'media_type', 'image'),
            num_frames=getattr(self.data_cfg, 'num_frames', 1),
            mm_mlp_adapter_type=getattr(self.cfg.mm_cfg, 'mm_mlp_adapter_type', 'linear'),
        )

        patch_dim = self.multimodal_cfg['patch_dim']
        height_num_patches = self.multimodal_cfg['crop_size'][0] // patch_dim
        width_num_patches = self.multimodal_cfg['crop_size'][1] // patch_dim
        self.num_media_latents = height_num_patches * width_num_patches

    def preprocess_media_tokens(self, sources: dict, cur_token_len: int, use_plain: bool = False):
        """
        Preprocesses multimodal sources based on the provided configuration.

        This function modifies the sources for multimodal data processing. It checks if the data is multimodal and
        adjusts the token lengths accordingly. It also handles the start and end tokens for images and replaces
        image tokens in conversations.

        Parameters:
        - sources (dict): A dictionary containing the multimodal sources to be processed.
        - multimodal_cfg (dict): A configuration dictionary specifying various options for multimodal processing.
          It includes keys like 'is_multimodal', 'use_im_start_end', and 'sep_image_conv_front'.
        - cur_token_len (int): The current length of tokens to be considered for image processing.
        - use_plain (bool, optional): A boolean flag to use plain image token replacement without additional processing.
          Defaults to False.

        Returns:
        - dict: The processed sources dictionary after applying multimodal preprocessing steps.
        """
        multimodal_cfg = self.multimodal_cfg
        is_multimodal = multimodal_cfg['is_multimodal']
        media_type = multimodal_cfg['media_type']
        image_token_len = cur_token_len
        if media_type == 'image':
            default_token = self.image_token
        elif media_type == 'video':
            default_token = self.video_token
        else:
            return sources

        if not is_multimodal:
            return sources

        num_patches = image_token_len
        if media_type == 'video':
            num_patches *= multimodal_cfg['num_frames']

        if multimodal_cfg['mm_mlp_adapter_type'] == 'mlp_downsample':
            num_patches //= 4

        if multimodal_cfg['use_im_start_end']:
            replace_token = self.image_patch_token * num_patches
        else:
            replace_token = self.image_patch_token * (num_patches - 2)

        replace_token = self.im_start_token + replace_token + self.im_end_token

        for source in sources:
            conversation = source['conversations']
            if use_plain:
                assert default_token in conversation[0]['value']
                conversation[0]['value'] = default_token
            for turn in conversation:
                turn["value"] = turn["value"].replace(default_token, replace_token)

        return sources
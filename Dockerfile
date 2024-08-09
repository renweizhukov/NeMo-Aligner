# CUDA 12.3
FROM nvcr.io/nvidia/pytorch:24.02-py3

### config tags
ARG APEX_TAG=810ffae374a2b9cb4b5c5e28eaeca7d7998fca0c
ARG TE_TAG=bfe21c3d68b0a9951e5716fb520045db53419c5e
ARG MLM_TAG=fbb375d4b5e88ce52f5f7125053068caff47f93f
ARG NEMO_BRANCH=nemo-aligner-trt-fix
ARG TENSOR_LLM_BRANCH=nemo-aligner-trt-fix
ARG PYTRITON_VERSION=0.4.1
ARG PROTOBUF_VERSION=4.24.4
ARG ALIGNER_COMMIT=v0.3.0.trtllm-fix

# if you get errors building TE or Apex, decrease this to 4
ARG MAX_JOBS=8

# needed in case git complains that it can't detect a valid email, this email is fake but works
RUN git config --global user.email "worker@nvidia.com"

WORKDIR /opt

# install TransformerEngine
RUN pip uninstall -y transformer-engine && \
    git clone https://github.com/NVIDIA/TransformerEngine.git && \
    cd TransformerEngine && \
    if [ ! -z $TE_TAG ]; then \
        git fetch origin $TE_TAG && \
        git checkout FETCH_HEAD; \
    fi && \
    git submodule init && git submodule update && \
    NVTE_FRAMEWORK=pytorch NVTE_WITH_USERBUFFERS=1 MPI_HOME=/usr/local/mpi pip install .

# install latest apex
RUN pip uninstall -y apex && \
    git clone https://github.com/NVIDIA/apex && \
    cd apex && \
    if [ ! -z $APEX_TAG ]; then \
        git fetch origin $APEX_TAG && \
        git checkout FETCH_HEAD; \
    fi && \
    pip install -e . -v --no-build-isolation --disable-pip-version-check --no-cache-dir --config-settings "--build-option=--cpp_ext --cuda_ext --fast_layer_norm --distributed_adam --deprecated_fused_adam --group_norm"

# place any util pkgs here
RUN pip install --upgrade-strategy only-if-needed nvidia-pytriton==$PYTRITON_VERSION
RUN pip install -U --no-deps protobuf==$PROTOBUF_VERSION
RUN pip install --upgrade-strategy only-if-needed jsonlines

# NeMo with customized fix
RUN git clone https://github.com/renweizhukov/NeMo.git && \
    cd NeMo && \
    git pull && \
    if [ ! -z $NEMO_BRANCH ]; then \
        git checkout $NEMO_BRANCH; \
    fi && \
    pip uninstall -y nemo_toolkit sacrebleu && \
    pip install -e ".[nlp]" && \
    cd nemo/collections/nlp/data/language_modeling/megatron && make

# MLM
RUN pip uninstall -y megatron-core && \
    git clone https://github.com/NVIDIA/Megatron-LM.git && \
    cd Megatron-LM && \
    git pull && \
    if [ ! -z $MLM_TAG ]; then \
        git fetch origin $MLM_TAG && \
        git checkout FETCH_HEAD; \
    fi && \
    pip install -e .

# NeMo Aligner with customized Dockerfile
RUN git clone https://github.com/renweizhukov/NeMo-Aligner.git && \
    cd NeMo-Aligner && \
    git pull && \
    if [ ! -z $ALIGNER_COMMIT ]; then \
        git fetch origin $ALIGNER_COMMIT && \
        git checkout FETCH_HEAD; \
    fi && \
    pip install --no-deps -e .

# Git LFS
RUN curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.deb.sh | bash && \
    apt-get install git-lfs && \
    git lfs install

# TRTLLM-0.9.0 with customized fix
RUN git clone https://github.com/renweizhukov/TensorRT-LLM.git && \
    cd TensorRT-LLM && \
    if [ ! -z $TENSOR_LLM_BRANCH ]; then \
        git checkout $TENSOR_LLM_BRANCH; \
    fi && \
    . docker/common/install_tensorrt.sh && \
    python3 ./scripts/build_wheel.py --trt_root /usr/local/tensorrt 

RUN cd TensorRT-LLM && \
    pip install ./build/tensorrt_llm*.whl
ENV LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/cuda-12.3/compat/lib.real/

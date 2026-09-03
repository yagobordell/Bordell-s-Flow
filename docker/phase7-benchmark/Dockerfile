FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    TORCH_CUDA_ARCH_LIST=12.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        git-lfs \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -c \
    "import sys, torch, torchvision, torchaudio; \
     assert sys.version_info >= (3, 12), sys.version; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert torchvision.__version__.startswith('0.26.0'), torchvision.__version__; \
     assert torchaudio.__version__.startswith('2.11.0'), torchaudio.__version__; \
     print('base_python=', sys.version); \
     print('base_torch=', torch.__version__); \
     print('base_cuda=', torch.version.cuda)"

ARG LTX_COMMIT=a95ab856bf29407b6b066ede0abe1846050db56c

RUN git clone \
        --filter=blob:none \
        https://github.com/Lightricks/LTX-2.git \
        /opt/ltx \
    && git -C /opt/ltx checkout "${LTX_COMMIT}"

RUN printf '%s\n' \
        'torch==2.11.0' \
        'torchvision==0.26.0' \
        'torchaudio==2.11.0' \
        > /opt/torch-constraints.txt

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --constraint /opt/torch-constraints.txt \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        --editable /opt/ltx/packages/ltx-core \
        --editable /opt/ltx/packages/ltx-pipelines

COPY . /opt/factory

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --constraint /opt/torch-constraints.txt \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        --editable /opt/factory \
        "boto3>=1.35,<2" \
        "huggingface-hub[cli]"

RUN mkdir -p /opt/ltx/.venv/bin \
    && ln -sf "$(command -v python3)" /opt/ltx/.venv/bin/python \
    && ln -sf "$(command -v python3)" /opt/ltx/.venv/bin/python3

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --no-deps \
        --find-links https://whl.natten.org \
        "natten==0.21.6+torch2110cu128"

COPY docker/phase7-benchmark/result_server.py \
    /opt/factory/result_server.py

COPY docker/phase7-benchmark/start_benchmark.sh \
    /opt/factory/start_benchmark.sh

RUN chmod +x /opt/factory/start_benchmark.sh \
    && test -s /opt/factory/docker/phase7-benchmark/assets/shot_001.png

RUN python3 -c \
    "import importlib.util, torch, ltx_pipelines.distilled; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert importlib.util.find_spec('natten') is not None; \
     import natten; \
     print('final_torch=', torch.__version__); \
     print('final_cuda=', torch.version.cuda); \
     print('natten=', natten.__version__); \
     print('ltx_pipeline=OK')"

RUN mkdir -p \
    /workspace/models \
    /workspace/public/results \
    /workspace/output \
    /workspace/logs

ENV PATH=/opt/ltx/.venv/bin:${PATH}

WORKDIR /workspace

EXPOSE 8080

CMD ["bash"]

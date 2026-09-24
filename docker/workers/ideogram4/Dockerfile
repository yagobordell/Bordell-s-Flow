FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime

ARG IDEOGRAM4_RUNTIME_REVISION=990fe1c4e950bb9e9dc90e01c0ad98ba434f83c2

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DOWNLOAD_TIMEOUT=120 \
    HF_HUB_ETAG_TIMEOUT=30 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    IDEOGRAM_MODEL_ROOT=/workspace/models/ideogram4 \
    IDEOGRAM_MODEL_LOCAL_SNAPSHOT=/workspace/models/ideogram4/snapshot \
    IDEOGRAM_MODEL_REPOSITORY=ideogram-ai/ideogram-4-nf4 \
    IDEOGRAM_MODEL_REVISION=main \
    IDEOGRAM_DEVICE=cuda \
    IDEOGRAM_DTYPE=bfloat16 \
    IDEOGRAM_SAMPLER_PRESET=V4_QUALITY_48 \

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -c \
    "import sys, torch; \
     assert sys.version_info >= (3, 12), sys.version; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda"

RUN git clone https://github.com/ideogram-oss/ideogram4.git /opt/ideogram4 \
    && cd /opt/ideogram4 \
    && git checkout "${IDEOGRAM4_RUNTIME_REVISION}"

RUN python3 - <<'PY'
from pathlib import Path

path = Path("/opt/ideogram4/src/ideogram4/pipeline_ideogram4.py")
text = path.read_text(encoding="utf-8")
helper = '''

def _resolve_model_file(repo_id: str, filename: str) -> str:
  local_repo = Path(repo_id)
  if local_repo.is_dir():
    local_file = local_repo / filename
    if not local_file.is_file():
      raise EntryNotFoundError(f"Model file is missing from local snapshot: {local_file}")
    return str(local_file)
  return hf_hub_download(repo_id=repo_id, filename=filename)
'''
local_config = '''    config = config or Ideogram4PipelineConfig()
    local_snapshot = Path(os.environ["IDEOGRAM_MODEL_LOCAL_SNAPSHOT"])
    if not local_snapshot.is_dir():
      raise FileNotFoundError(
        f"Ideogram local model snapshot is unavailable: {local_snapshot}"
      )
    config.weights_repo = str(local_snapshot)
    transformer_config = transformer_config or Ideogram4Config()
'''
replacements = [
    ("import json\nimport warnings\n", "import json\nimport os\nimport warnings\n", 1),
    (
        "from posixpath import dirname as _posix_dirname, join as _posix_join\n",
        "from posixpath import dirname as _posix_dirname, join as _posix_join\nfrom pathlib import Path\n",
        1,
    ),
    (
        "from huggingface_hub.errors import EntryNotFoundError",
        "from huggingface_hub.errors import EntryNotFoundError, LocalEntryNotFoundError",
        1,
    ),
    ("except EntryNotFoundError:", "except (EntryNotFoundError, LocalEntryNotFoundError):", 2),
    ("hf_hub_download(", "_resolve_model_file(", 6),
    ("\ndef _load_subfolder_state_dict(\n", helper + "\ndef _load_subfolder_state_dict(\n", 1),
    (
        "    config = config or Ideogram4PipelineConfig()\n"
        "    transformer_config = transformer_config or Ideogram4Config()\n",
        local_config,
        1,
    ),
]
for old, new, count in replacements:
    if text.count(old) != count:
        raise RuntimeError("Unexpected Ideogram 4 runtime layout")
    text = text.replace(old, new)
path.write_text(text, encoding="utf-8")
PY

RUN cd /opt/ideogram4 \
    && python3 -m pip install --break-system-packages --no-cache-dir .

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        '/opt/factory[gpu]' \
        'huggingface-hub[cli]' \
        'hf-xet==1.6.0'

COPY docker/workers/ideogram4/download_models.sh /usr/local/bin/download-models
COPY docker/workers/ideogram4/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/download-models \
        /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents \
        /workspace/models/ideogram4 \
        /workspace/.cache/huggingface \
        /tmp/ai-video-factory \
        /opt/factory/data/inference-local \
    && chown --recursive worker:worker \
        /workspace \
        /tmp/ai-video-factory \
        /opt/factory/data

RUN python3 -c \
    "import bitsandbytes, ideogram4, torch, transformers; \
     from importlib.metadata import version; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert version('hf-xet') == '1.6.0', version('hf-xet'); \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('transformers=', transformers.__version__); \
     print('bitsandbytes=', bitsandbytes.__version__); \
     print('hf-xet=', version('hf-xet')); \
     print('ideogram4=', ideogram4.__file__)"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=3m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]

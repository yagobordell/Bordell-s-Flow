FROM python:3.12-slim-bookworm AS salad-worker

ARG SALAD_WORKER_VERSION=v0.7.0
ARG SALAD_WORKER_SHA256=074a329cf6462e77fc7b72100f59d8a690831456d9420186a834a8f30634c9e4

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && curl --fail --show-error --location \
        "https://github.com/SaladTechnologies/salad-cloud-job-queue-worker/releases/download/${SALAD_WORKER_VERSION}/salad-http-job-queue-worker_x86_64.tar.gz" \
        --output /tmp/salad-worker.tar.gz \
    && echo "${SALAD_WORKER_SHA256}  /tmp/salad-worker.tar.gz" | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/salad-worker.tar.gz --directory /usr/local/bin \
        salad-http-job-queue-worker \
    && chmod 0755 /usr/local/bin/salad-http-job-queue-worker

FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime

ARG IDEOGRAM4_RUNTIME_REVISION=990fe1c4e950bb9e9dc90e01c0ad98ba434f83c2

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    HF_HUB_OFFLINE=1 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    IDEOGRAM_MODEL_ROOT=/workspace/models/ideogram4 \
    IDEOGRAM_MODEL_LOCAL_SNAPSHOT=/workspace/models/ideogram4/snapshot \
    IDEOGRAM_MODEL_REPOSITORY=ideogram-ai/ideogram-4-nf4 \
    IDEOGRAM_MODEL_REVISION=main \
    IDEOGRAM_DEVICE=cuda \
    IDEOGRAM_DTYPE=bfloat16 \
    IDEOGRAM_SAMPLER_PRESET=V4_QUALITY_48 \
    SALAD_QUEUE_ENABLED=false

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
import_line = "from huggingface_hub.errors import EntryNotFoundError"
except_line = "except EntryNotFoundError:"
hub_call = "hf_hub_download("
imports_anchor = "import json\nimport warnings\n"
pathlib_anchor = "from posixpath import dirname as _posix_dirname, join as _posix_join\n"
helper_anchor = "\ndef _load_subfolder_state_dict(\n"
config_anchor = (
    "    config = config or Ideogram4PipelineConfig()\n"
    "    transformer_config = transformer_config or Ideogram4Config()\n"
)
if text.count(import_line) != 1 or text.count(except_line) != 2:
    raise RuntimeError("Unexpected Ideogram 4 Hugging Face fallback layout")
if text.count(hub_call) != 6:
    raise RuntimeError("Unexpected Ideogram 4 Hugging Face download layout")
if text.count(imports_anchor) != 1 or text.count(pathlib_anchor) != 1:
    raise RuntimeError("Unexpected Ideogram 4 import layout")
if text.count(helper_anchor) != 1 or text.count(config_anchor) != 1:
    raise RuntimeError("Unexpected Ideogram 4 pipeline construction layout")
text = text.replace(
    import_line,
    "from huggingface_hub.errors import EntryNotFoundError, LocalEntryNotFoundError",
)
text = text.replace(
    except_line,
    "except (EntryNotFoundError, LocalEntryNotFoundError):",
)
text = text.replace(imports_anchor, "import json\nimport os\nimport warnings\n")
text = text.replace(pathlib_anchor, pathlib_anchor + "from pathlib import Path\n")
text = text.replace(hub_call, "_resolve_model_file(")
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
text = text.replace(helper_anchor, helper + helper_anchor)
local_config = (
    "    config = config or Ideogram4PipelineConfig()\n"
    "    local_snapshot = Path(os.environ[\"IDEOGRAM_MODEL_LOCAL_SNAPSHOT\"])\n"
    "    if not local_snapshot.is_dir():\n"
    "      raise FileNotFoundError(\n"
    "        f\"Ideogram local model snapshot is unavailable: {local_snapshot}\"\n"
    "      )\n"
    "    config.weights_repo = str(local_snapshot)\n"
    "    transformer_config = transformer_config or Ideogram4Config()\n"
)
text = text.replace(config_anchor, local_config)
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
        'huggingface-hub[cli]'

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/ideogram4/download_models.sh /usr/local/bin/download-models
COPY docker/workers/ideogram4/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/salad-http-job-queue-worker \
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
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('transformers=', transformers.__version__); \
     print('bitsandbytes=', bitsandbytes.__version__); \
     print('ideogram4=', ideogram4.__file__)"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=3m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]

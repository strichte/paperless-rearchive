# LLM Server setup

## Introduction

I tested a few configurations. The Chandra models run faster on `vllm`, but I prefer the GGUF version under `llama-swap` — it lets me share the same GPU with other tasks and swap models in and out dynamically.

To give an idea of real-life speeds, I tested tag `re-ocr-all` on a complex 4-page document: a scan from old council files with a mix of hand-filled forms, handwritten letters, and a technical drawing. The table shows total OCR time, including inference and processing overhead.

For my documents I didn't notice a quality difference between the full model and the quantized version.

| Server | REARCHIVE_OCR_CONCURRENCY | Model | OCR duration (s) |
| :----- | ------------------------: | :---- | ---------------: |
| llama-swap:v255-cuda13-b10902[^1] | 4 | chandra-ocr-2-q8 | 17.8 |
| llama-swap:v255-cuda13-b10902[^1] | 4 | chandra-ocr-2-bf16 | 23.1 |
| vllm:v0.30.0 | 4 | chandra-ocr-2-FP8-dynamic | 16.6 |
| vllm:v0.30.0 | 4 | chandra-ocr-2 | 18.2 |

[^1]: Runs `llama-server` 0.4.0-dev (build 10902, commit df03399b8) with GGUF models.

So for the quantized version we're looking at 17.8 s vs 16.6 s — `llama-swap` vs `vllm`, a 7.2% increase. Not much, and I'll take the flexibility.

## Prerequisites

- Docker + Docker Compose v2.
- NVIDIA driver and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) — `docker run --gpus all … nvidia-smi` must work.

## `llama-swap` Setup

Create the directories and download the models:
```bash
mkdir -p ~/docker/llama-swap/models/chandra-ocr-2
cd ~/docker/llama-swap/models/chandra-ocr-2

# Q8_0 quantized (~5.16 GB)
curl -L -C - -o chandra-ocr-2.Q8_0.gguf \
  https://huggingface.co/prithivMLmods/chandra-ocr-2-GGUF/resolve/main/chandra-ocr-2.Q8_0.gguf

# Unquantized BF16 (~9.7 GB) — lossless repackaging of the bf16 checkpoint weights
curl -L -C - -o chandra-ocr-2.BF16.gguf \
  https://huggingface.co/prithivMLmods/chandra-ocr-2-GGUF/resolve/main/chandra-ocr-2.BF16.gguf

# Vision projector (shared by both variants — see note below)
curl -L -C - -o chandra-ocr-2.mmproj-f16.gguf \
  https://huggingface.co/prithivMLmods/chandra-ocr-2-GGUF/resolve/main/chandra-ocr-2.mmproj-f16.gguf

# Hard link for the bf16 variant (same file)
ln chandra-ocr-2.mmproj-f16.gguf chandra-ocr-2.mmproj-bf16.gguf
```

**mmproj note:** the repo's `mmproj-bf16.gguf` is byte-identical to `mmproj-f16.gguf` (same sha256 — one projector ships with every quant). We keep a single physical copy: `chandra-ocr-2.mmproj-bf16.gguf` is a **hardlink** to `chandra-ocr-2.mmproj-f16.gguf`. Re-create it after any re-download with the `ln` command above.

In the same directory as your `docker-compose.yaml`, create a `.env` file with your Chandra API key:

```bash
# .env — read by docker compose for interpolation
CHANDRA_API_KEY=super-secret_key
```

Any random string works — it's just a bearer token shared between the sidecar and the server. You'll need the same key when configuring the `paperless-rearchive` sidecar.

Create `~/docker/llama-swap/config.yaml`. It defines both variants — they can coexist: llama-swap loads a model on the first request and unloads it after `ttl: 600` (10 min idle), so only one model occupies the GPU at a time:
```yaml
models:
  "chandra-ocr-2-q8":
    description: "Datalab Chandra OCR 2 (5B vision model) - document/image OCR to markdown"
    ttl: 600
    cmd: |
      llama-server
      -m /models/chandra-ocr-2/chandra-ocr-2.Q8_0.gguf
      --mmproj /models/chandra-ocr-2/chandra-ocr-2.mmproj-f16.gguf
      --port ${PORT}
      --api-key ${env.CHANDRA_API_KEY}
      -ngl 999
      --parallel 1
      --flash-attn on
      --ctx-size 18000
      --temp 0.0
      --jinja
      --chat-template-kwargs '{"enable_thinking":false}'
  "chandra-ocr-2-bf16":
    description: "Datalab Chandra OCR 2 (5B vision model) - unquantized BF16, max fidelity"
    ttl: 600
    cmd: |
      llama-server
      -m /models/chandra-ocr-2/chandra-ocr-2.BF16.gguf
      --mmproj /models/chandra-ocr-2/chandra-ocr-2.mmproj-bf16.gguf
      --port ${PORT}
      --api-key ${env.CHANDRA_API_KEY}
      -ngl 999
      --parallel 1
      --flash-attn on
      --ctx-size 18000
      --temp 0.0
      --jinja
      --chat-template-kwargs '{"enable_thinking":false}'
```

Both entries run with `--temp 0.0` (Chandra expects greedy decoding — llama-server's default of 0.8 makes output nondeterministic) and `--chat-template-kwargs '{"enable_thinking":false}'` (GGUF builds re-enable thinking otherwise, which breaks the output).

Create `docker-compose.yaml`:
```yaml
services:
  llama-swap:
    image: ghcr.io/mostlygeek/llama-swap:v255-cuda13-b10902 # or :cpu
    container_name: llama-swap
    ports:
      - "8110:8080"
    volumes:
      - ~/docker/llama-swap/models:/models
      - ~/docker/llama-swap/cache:/root/.cache
      - ~/docker/llama-swap/config.yaml:/app/config.yaml
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      # - NVIDIA_DRIVER_CAPABILITIES=compute,utility
      # Resolved by llama-swap's ${env.CHANDRA_API_KEY} (chandra models' --api-key).
      # Compose interpolates this from ./.env — same key chandra-server uses.
      - CHANDRA_API_KEY=${CHANDRA_API_KEY:?missing CHANDRA_API_KEY in .env}
    restart: unless-stopped
    command: --config /app/config.yaml --listen 0.0.0.0:8080
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Start it and check that it responds:

```bash
docker compose up -d
curl http://localhost:8110/v1/models -H "Authorization: Bearer super-secret_key"
```

llama-swap answers on port `8110`; the first request triggers the model load, so give it a few seconds.

## `vLLM` Setup

The `hf` CLI ships with `huggingface_hub` (`pip install -U huggingface_hub`).

Create the directories and download the models:
```bash
mkdir -p ~/docker/vllm/models/chandra-ocr-2/
mkdir -p ~/docker/vllm/vllm-cache/chandra-ocr-2
mkdir -p ~/docker/vllm/models/chandra-ocr-2-FP8-dynamic
mkdir -p ~/docker/vllm/vllm-cache/chandra-ocr-2-FP8-dynamic
hf download datalab-to/chandra-ocr-2 --local-dir ~/docker/vllm/models/chandra-ocr-2
hf download dangvansam/chandra-ocr-2-FP8-dynamic --local-dir ~/docker/vllm/models/chandra-ocr-2-FP8-dynamic
```

In the same directory as your `docker-compose.yaml`, create the same `.env` as above:

```bash
# .env — read by docker compose for interpolation
CHANDRA_API_KEY=super-secret_key
```

Create `docker-compose.yaml` with **one** of the two services shown below — don't run both: they bind the same host port `8000` and each reserves a GPU.
```yaml
services:
  chandra-server-fp8:
    image: vllm/vllm-openai:v0.30.0
    restart: unless-stopped
    command:
      # Point to the LOCAL model directory inside the container
      - /models/chandra-ocr-2-FP8-dynamic
      - --served-model-name=chandra-ocr-2-FP8-dynamic
      - --api-key=${CHANDRA_API_KEY:?missing CHANDRA_API_KEY in .env}
      - --max-model-len=16384
      - --max-num-seqs=64
      - --max-num-batched-tokens=16384
      - --kv-cache-dtype=fp8
      - --gpu-memory-utilization=0.85
      - --enable-prefix-caching
      - --enable-chunked-prefill
      - --trust-remote-code
      - --mm-processor-kwargs={"min_pixels":3136,"max_pixels":6291456}
    ports:
      - "8000:8000"
    ipc: host
    volumes:
      # Mount your local model directory into the container (read-only)
      - ~/docker/vllm/models/chandra-ocr-2-FP8-dynamic:/models/chandra-ocr-2-FP8-dynamic:ro
      - ~/docker/vllm/vllm-cache/chandra-ocr-2-FP8-dynamic:/root/.cache/vllm
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: ["gpu"]
              count: 1
  
  chandra-server:
    image: vllm/vllm-openai:v0.30.0
    restart: unless-stopped
    command:
      # Point to the LOCAL model directory inside the container
      - /models/chandra-ocr-2
      - --served-model-name=chandra-ocr-2
      - --api-key=${CHANDRA_API_KEY:?missing CHANDRA_API_KEY in .env}
      - --dtype=bfloat16
      - --max-model-len=18000
      - --max-num-seqs=16
      - --max-num-batched-tokens=2048
      - --gpu-memory-utilization=0.85
      - --enable-prefix-caching
      - --no-enforce-eager
      - --mm-processor-kwargs={"min_pixels":3136,"max_pixels":6291456}
    ports:
      - "8000:8000"
    ipc: host
    volumes:
      # Mount your local model directory into the container (read-only)
      - ~/docker/vllm/models/chandra-ocr-2:/models/chandra-ocr-2:ro
      - ~/docker/vllm/vllm-cache/chandra-ocr-2:/root/.cache/vllm
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: ["gpu"]
              count: 1
```

Start it and check that it responds:

```bash
docker compose up -d
curl http://localhost:8000/v1/models -H "Authorization: Bearer super-secret_key"
```

## Connect the sidecar

Point `paperless-rearchive` at whichever server you picked:

```yaml
environment:
  # llama-swap, same compose network:   http://llama-swap:8080/v1
  # llama-swap, via host port:          http://<host>:8110/v1
  # vllm, same compose network:         http://chandra-server:8000/v1
  PAPERLESS_CHANDRA_SERVER_URL: "http://llama-swap:8080/v1"
  PAPERLESS_CHANDRA_MODEL_NAME: "chandra-ocr-2-q8"   # llama-swap config key or vllm --served-model-name
  PAPERLESS_CHANDRA_API_KEY: "super-secret_key"       # same CHANDRA_API_KEY from .env
```

`PAPERLESS_CHANDRA_MODEL_NAME` must match a name the server actually advertises — a mismatch aborts the poll cycle. See the [README configuration reference](../README.md#configuration-reference) for all options.
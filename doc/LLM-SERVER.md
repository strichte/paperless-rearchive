# LLM Server setup

## Introduction
I tested a few configurations. While the Chandra models run faster on `vllm`, I prefer running the GGUF version in `llama-swap`. It allows me to easily use the same GPU for other tasks and swap models in and out dynamically.

To give an idea of real life speeds, I tested tag `re-ocr-all` on a complex 4 page document. It is a scan from old council files with a mix of forms filled out by hand, hand written letters, and a technical drawing. The table shows the total OCR time, which includes inference and processing overhead.

For my documents I didn't notice a quality diofferene between to full model and the quantisized version.

|Server | REARCHIVE_OCR_CONCURRENCY | Model | OCR Duration / sec |
| :-------- | --------: | :-------- | --------: |
| llama-swap:v255-cuda13-b10902<sup>[1](#note1)</sup> | 4|  chandra-ocr-2-q8 | 17.8 |
| llama-swap:v255-cuda13-b10902<sup>[1](#note1)</sup> | 4|  chandra-ocr-2-bf16  | 23.1 |
| vllm:v0.30.0 | 4 | chandra-ocr-2-FP8-dynamic | 16.6 |
| vllm:v0.30.0 | 4 | chandra-ocr-2 | 18.2 |


<a name="note1">1</a>: Running `llama-server` version 0.4.0-dev (build 10902, commit df03399b8) runnin GGUF model

So for the quntrisized version we are looking at 17.8 vs 16.6 seconds for `llama-swap` vs `vllm`. A 7.2% increase. 

## `llama-swap` Setup

Create directories and download models:
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

# Hard link vor vision projector
ln chandra-ocr-2.mmproj-f16.gguf chandra-ocr-2.mmproj-bf16.gguf
```

**mmproj note:** the repo's `mmproj-bf16.gguf` is byte-identical to
`mmproj-f16.gguf`. This directory keeps a single physical copy; `chandra-ocr-2.mmproj-bf16.gguf`
is a **hardlink** to `chandra-ocr-2.mmproj-f16.gguf`.

In the same directory as your `docker-compose.yanl` create `.env` and set your Chandra API key in it:
```bash
export CHANDRA_API_KEY=super-secret_key
```
You'll need the same key when configuring your `paparless-rearchive` sidecar container.

The `docker-compose.yaml` shows the config for both the full and the quantisized model. Obviously only run one at a time.

Create `~/docker/llama-swap/config.yaml`:
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

## `vLLM` Setup
Create directories and download models:
```bash
mkdir -p ~/docker/vllm/models/chandra-ocr-2/
mkdir -p ~/docker/vllm/vllm-cache/chandra-ocr-2
mkdir -p ~/docker/vllm/models/chandra-ocr-2-FP8-dynamic
mkdir -p ~/docker/vllm/vllm-cache/chandra-ocr-2-FP8-dynamic
hf download datalab-to/chandra-ocr-2 --local-dir ~/docker/vllm/models/chandra-ocr-2
hf download dangvansam/chandra-ocr-2-FP8-dynamic --local-dir ~/docker/vllm/models/chandra-ocr-2-FP8-dynamic
models/chandra-ocr-2/
```
In the same directory as your `docker-compose.yanl` create `.env` and set your Chandra API key in it:
```bash
export CHANDRA_API_KEY=super-secret_key
```
You'll need the same key when configuring your `paparless-rearchive` sidecar container.

The `docker-compose.yaml` shows the config for both the full and the quantisized model. Obviously only run one at a time.
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
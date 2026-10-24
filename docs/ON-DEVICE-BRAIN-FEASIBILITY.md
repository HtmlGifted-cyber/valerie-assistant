# Feasibility Report: On-Device LLM Brain for Valerie on Low-End Android (T-Mobile REVVL 6)

**Target Device:** T-Mobile REVVL 6 5G (MediaTek Dimensity 700, ~4GB RAM, Android 12/13)  
**Goal:** Run Valerie completely offline and independently on-device using a tiny LLM + local retrieval system.  
**Date:** October 2026  

---

## 1. Candidate Models for 4GB RAM (Q4 Quants)

To run a large language model locally on a 4GB RAM Android device, the model must fit comfortably within the system constraints. Android OS and background services consume approximately 1.5GB to 2GB of RAM, leaving roughly **1.5GB to 2.0GB of free RAM** for the LLM runtime and context window (KV cache). Therefore, **1B to 1.7B parameter models quantized to 4-bit (Q4_K_M)** are the sweet spot, as their file sizes and memory footprints range from 800MB to 1.2GB.

| Model | Size (Q4_K_M) | RAM Footprint (Model + KV Cache) | Estimated Speed (MediaTek Dimensity 700) | Quality & Capability Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Qwen2.5-1.5B-Instruct** | ~1.0 GB | ~1.3 GB | ~8 – 12 tokens/sec | Excellent instruction following, multilingual, strong reasoning for its size. Highly recommended. |
| **Llama-3.2-1B-Instruct** | ~700 MB | ~1.0 GB | ~15 – 20 tokens/sec | Extremely fast, compact, Meta-backed, decent tool use and general conversation. |
| **SmolLM2-1.7B-Instruct** | ~1.1 GB | ~1.4 GB | ~7 – 10 tokens/sec | Trained on 11T tokens, specifically optimized for on-device assistant tasks and code/reasoning. |
| **Gemma-3-1B-IT** *(or Gemma-2-2B)* | ~850 MB – 1.3 GB | ~1.1 – 1.6 GB | ~8 – 14 tokens/sec | High quality output, though Gemma models can sometimes be slightly heavier on memory bandwidth during prefill. |

### Hardware Profile: MediaTek Dimensity 700
- **CPU:** Octa-core (2x 2.2 GHz Cortex-A76 & 6x 2.0 GHz Cortex-A55)
- **GPU:** Mali-G57 MC2
- **Memory Bandwidth:** LPDDR4X (relatively narrow bus), meaning generation speed is **memory-bandwidth bound**. Q4 quantization is mandatory to keep weights small and fit within cache/bandwidth limits. Expect CPU-based execution via llama.cpp (with NEON optimization) to outperform mobile GPU backends for models under 2B parameters due to kernel launch overhead.

---

## 2. On-Device Runtimes for Android Integration

To embed the LLM directly inside the Valerie Android app without requiring users to install separate third-party launcher apps (like PocketPal AI or Termux), we evaluate the following embedding runtimes:

1. **llama.cpp (via Java Native Interface / JNI or mobile wrappers like `llama.rn` / custom Android NDK wrapper):**
   - *Pros:* Industry standard, excellent support for GGUF format, highly optimized ARM NEON CPU kernels, minimal binary footprint (~5–15MB library size), full control over prompt templates and KV cache.
   - *Cons:* Requires writing JNI bridge code or using community bindings.
   - *Verdict:* **Best choice for custom app embedding.**

2. **MediaPipe LLM Inference API (Google):**
   - *Pros:* Native Android SDK support (`com.google.mediapipe:tasks-genai`), hardware acceleration via GPU/NPU delegates on supported chipsets, very clean Kotlin API.
   - *Cons:* Requires models in MediaPipe's specific `.bin` Task format (though conversion tools exist from PyTorch/GGUF), less flexible than raw llama.cpp for custom grammar/sampling tweaks.
   - *Verdict:* **Strong runner-up** if native Kotlin integration with minimal C++ compilation is preferred.

3. **ExecuTorch (PyTorch / Meta):**
   - *Pros:* Deep PyTorch ecosystem integration, designed for edge devices.
   - *Cons:* Still maturing for arbitrary autoregressive LLMs on lower-end MediaTek chips compared to llama.cpp.

4. **MLC LLM (Machine Learning Compilation):**
   - *Pros:* TVM-based, highly optimized compiled kernels for mobile GPUs.
   - *Cons:* Large binary size, complex build pipeline, heavier dependency tree.

---

## 3. App Bundle Size & Distribution Strategy

To keep the Valerie Android app lightweight and compliant with distribution goals:

- **Target App Binary Size:** **< 50 MB** (achieved by packaging only the native runtime libraries, UI code, Kotlin/Java wrappers, and retrieval logic).
- **Model Distribution:** Do **not** bundle the 1GB model inside the APK (which would inflate the download size to over 1GB and violate Play Store / sideloading efficiency best practices). 
- **First-Launch Experience:** 
  1. The app installs quickly under ~40MB.
  2. On first boot, Valerie guides the user through downloading their chosen model (e.g., Qwen2.5-1.5B-Instruct Q4_K_M or Llama-3.2-1B) directly from Hugging Face or a local server into app-private internal storage (`context.filesDir`).
  3. Optional quantized presets allow users to pick between "Speed (1B)" and "Smart (1.5B)".

---

## 4. The 'Small Model + Kiwix / Local Retrieval' Approach

On a constrained 1B–1.5B parameter model, relying solely on parametric memory leads to hallucinations or shallow answers. Valerie's architecture overcomes this using **Retrieval-Augmented Generation (RAG) paired with offline local storage**:

- **Offline Knowledge Base (Kiwix / ZIM files):** 
  - Valerie can interface with compressed `.zim` archives (Wikipedia subsets, medical wikis, offline manuals, or local markdown notes).
- **Lightweight On-Device Retrieval Pipeline:**
  - Instead of running a heavy embedding model (which would consume extra RAM), lightweight lexical search (SQLite FTS5 full-text search or BM25 over local JSON/markdown chunks) runs instantly on the phone.
- **Workflow:**
  1. User asks Valerie a question offline.
  2. Valerie queries the local SQLite/FTS5 knowledge store for relevant snippets.
  3. Valerie constructs a tight prompt:  
     `[Context: <retrieved snippet>] [User Question: <query>] [Instructions: Answer concisely based only on context.]`
  4. The 1.5B model generates an accurate, grounded response in ~1–2 seconds without needing an active internet connection or cloud API.

---

## Sources & References
- *llama.cpp Android Benchmarks & NDK Integration:* [ggerganov/llama.cpp](https://github.com/ggerganov/llama.cpp)
- *Google MediaPipe LLM Inference Guide:* [Google AI Edge / MediaPipe Documentation](https://ai.google.dev/edge/mediapipe/solutions/genai/llm_inference)
- *Qwen2.5 Technical Report & Mobile quants:* [Hugging Face - Qwen/Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF)
- *Llama 3.2 1B specs:* [Meta Llama 3.2 Model Card](https://github.com/meta-llama/llama-models)
- *Kiwix Offline Reader & ZIM format:* [Kiwix Open Source ZIM tools](https://kiwix.org/)

---
pipeline_tag: text-generation
base_model:
- Qwen/Qwen3-Next-80B-A3B-Instruct
license: apache-2.0
library_name: Model Optimizer
tags:
- nvidia
- ModelOpt
- Qwen3
- quantized
- NVFP4
- nvfp4
---

# Model Overview

## Description:
The NVIDIA Qwen3-Next-80B-A3B-Instruct NVFP4 model is the quantized version of Alibaba's Qwen3-Next-80B-A3B-Instruct model, which is an auto-regressive language model that uses an optimized transformer architecture. For more information, please check [here](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct). The NVIDIA Qwen3-Next-80B-A3B-Instruct NVFP4 model is quantized with [TensorRT Model Optimizer](https://github.com/NVIDIA/TensorRT-Model-Optimizer).

This model is ready for commercial/non-commercial use.  <br>

## Third-Party Community Consideration
This model is not owned or developed by NVIDIA. This model has been developed and built to a third-party’s requirements for this application and use case; see link to Non-NVIDIA [(Qwen3-Next-80B-A3B-Instruct) Model Card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct).

### License/Terms of Use:
[Apache license 2.0](https://huggingface.co/datasets/choosealicense/licenses/blob/main/markdown/apache-2.0.md)

### Deployment Geography:
Global <br>

### Use Case:
Developers looking to take off the shelf pre-quantized models for deployment in AI Agent systems, chatbots, RAG systems, and other AI-powered applications. <br>

### Release Date:
Huggingface 12/29/2025 via https://huggingface.co/nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4 <br>

## Model Architecture:
**Architecture Type:** Transformers  <br>
**Network Architecture:** Qwen3NextForCausalLM <br>
**This model was developed based on [Qwen3-Next-80B-A3B-Instruct](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct) <br>
**Number of model parameters: Undisclosed. <br>

## Input:
**Input Type(s):** Text <br>
**Input Format(s):** String <br>
**Input Parameters:** 1D (One-Dimensional): Sequences <br>
**Other Properties Related to Input:** Context length 262,144 natively and extendable up to 1,010,000 tokens <br>

## Output:
**Output Type(s):** Text <br>
**Output Format:** String <br>
**Output Parameters:** 1D (One-Dimensional): Sequences <br>
**Other Properties Related to Output:** N/A <br>

Our AI models are designed and/or optimized to run on NVIDIA GPU-accelerated systems. By leveraging NVIDIA’s hardware (e.g. GPU cores) and software frameworks (e.g., CUDA libraries), the model achieves faster training and inference times compared to CPU-only solutions. <br>  

## Software Integration:
**Runtime Engine(s):** <br>
* TensorRT-LLM <br>

**Supported Hardware Microarchitecture Compatibility:** <br>
* NVIDIA Blackwell <br>

**Preferred Operating System(s):** <br>
* Linux <br>

The integration of foundation and fine-tuned models into AI systems requires additional testing using use-case-specific data to ensure safe and effective deployment. Following the V-model methodology, iterative testing and validation at both unit and system levels are essential to mitigate risks, meet technical and functional requirements, and ensure compliance with safety and ethical standards before deployment

## Model Version(s):
The model is quantized with nvidia-modelopt **v0.40.0**  <br>

## Training, Testing, and Evaluation Datasets:

## Calibration Dataset: 
** Link: [cnn_dailymail](https://huggingface.co/datasets/abisee/cnn_dailymail), [Nemotron-Post-Training-Dataset-v2](https://huggingface.co/datasets/nvidia/Nemotron-Post-Training-Dataset-v2) <br>
** Data collection method: Automated. <br>
** Labeling method: Automated. <br>

## Training Datasets:
** Data Collection Method by Dataset: Undisclosed <br>
** Labeling Method by Dataset: Undisclosed<br>
** Properties: Undisclosed

## Testing Dataset:
** Data Collection Method by Dataset: Undisclosed <br>
** Labeling Method by Dataset: Undisclosed <br>
** Properties: Undisclosed <br>

## Evaluation Dataset: 
* Datasets: MMLU Pro, GPQA Diamond, LiveCodeBench V6, SciCode, AIME 2025 <br>
** Data collection method: Hybrid: Automated, Human <br>
** Labeling method: Hybrid: Human, Automated <br>


## Inference:
**Acceleration Engine:** TensorRT-LLM <br>
**Test Hardware:** B200 <br>

## Post Training Quantization
This model was obtained by quantizing the weights and activations of Qwen3-Next-80B-A3B-Instruct to NVFP4 data type, ready for inference with TensorRT-LLM. Only the weights and activations of the linear operators within transformer blocks are quantized. This optimization reduces the number of bits per parameter from 16 to 4, reducing the disk size and GPU memory requirements by approximately 3.3x.

## Usage

### Deploy with TensorRT-LLM

To deploy the quantized checkpoint with [TensorRT-LLM](https://github.com/NVIDIA/TensorRT-LLM) LLM API, follow the sample codes below:

* LLM API sample usage:
```
from tensorrt_llm import LLM, SamplingParams, KvCacheConfig


def main():

    prompts = [
        "Hello, my name is",
        "The president of the United States is",
        "The capital of France is",
        "The future of AI is",
    ]
    sampling_params = SamplingParams(temperature=0.6, top_p=0.95)
    kv_cache_config = KvCacheConfig(enable_block_reuse=False)

    llm = LLM(model="nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4", tensor_parallel_size=4, kv_cache_config=kv_cache_config)

    outputs = llm.generate(prompts, sampling_params)

    # Print the outputs.
    for output in outputs:
        prompt = output.prompt
        generated_text = output.outputs[0].text
        print(f"Prompt: {prompt!r}, Generated text: {generated_text!r}")


# The entry point of the program needs to be protected for spawning processes.
if __name__ == '__main__':
    main()

```

### Evaluation
The accuracy benchmark results are presented in the table below:
<table>
  <tr>
   <td><strong>Precision</strong>
   </td>
   <td><strong>MMLU Pro</strong>
   </td>
   <td><strong>GPQA Diamond</strong>
   </td>
   <td><strong>LiveCodeBench V6</strong>
   </td>
   <td><strong>SciCode</strong>
   </td>
   <td><strong>AIME 2025</strong>
   </td>
  </tr>
  <tr>
   <td>FP8
   </td>
   <td>0.816
   </td>
   <td>0.736
   </td>
   <td>0.554
   </td>
   <td>0.357
   </td>
   <td>0.618
   </td>
  </tr>
  <tr>
   <td>NVFP4
   </td>
   <td>0.811
   </td>
   <td>0.725
   </td>
   <td>0.555
   </td>
   <td>0.343
   </td>
   <td>0.620
   </td>
  </tr>
  <tr>
</table>

> Baseline: [Qwen3-Next-80B-A3B-Instruct-FP8](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct-FP8).
> Benchmarked with temperature=0, top_p=1e-5, max num tokens 16384


## Ethical Considerations

NVIDIA believes Trustworthy AI is a shared responsibility and we have established policies and practices to enable development for a wide array of AI applications.  When downloaded or used in accordance with our terms of service, developers should work with their internal model team to ensure this model meets requirements for the relevant industry and use case and addresses unforeseen product misuse.

Please report model quality, risk, security vulnerabilities or NVIDIA AI Concerns [here](https://app.intigriti.com/programs/nvidia/nvidiavdp/detail).

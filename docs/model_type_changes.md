# Instructions for Model Type Improvement

## Objective

Update the `manage_models` function in `cli.py` to accurately distinguish between MLX and GGUF models instead of defaulting to "GGUF" if a `config.json` is missing or incorrectly identifying any model with a `config.json` as MLX.

## Proposed Design Changes

To accurately determine the model type, implement a multi-tiered check:

### 1. Local File Inspection (Fastest)

Before querying the Hub, inspect the files within the snapshot directory:

- **GGUF Detection**: Check if any file in the snapshot directory ends with `.gguf`. If so, classify as `GGUF`.
- **MLX Detection**:
  - Check if the directory contains a `config.json`.
  - If it does, check for specific keys that indicate MLX (e.g., `model_type` or specific architecture names associated with MLX).
  - Alternatively, check if the directory contains files typical of MLX weights (e.g., `.npz` or specific naming conventions).

### 2. Hugging Face Hub API Query (Fallback/Verification)

If local inspection is inconclusive, use the `huggingface_hub` library to query the model's metadata.

- Use `HfApi().model_info(repo_id)` to retrieve the model's tags and library information.
- **GGUF**: Check if `library == "gguf"` or if any tags contain `"gguf"`.
- **MLX**: Check if the `repo_id` starts with `mlx-community/` or if any tags contain `"mlx"`.

### 3. Implementation Details

- **Dependency**: Add `huggingface_hub` to the project dependencies if not already present (though it is likely a dependency of the `hf` command).
- **Refactoring**:
  - Create a helper function `determine_model_type(snapshot_path, repo_id)` to encapsulate this logic.
  - Update the loop in `manage_models` to call this helper.
  - Ensure that if a model cannot be identified as either MLX or GGUF, it is skipped or labeled as "Unknown" to prevent incorrect symlinking.

## Step-by-Step Instructions for the LLM

1.  **Import necessary modules**: Ensure `from huggingface_hub import HfApi` is available.
2.  **Create a helper function**: Define `get_model_type(snapshot_path, repo_id)`:
    - First, check for `.gguf` files in `snapshot_path`. If found, return `"GGUF"`.
    - Second, check for `config.json` in `snapshot_path`. If it exists and contains specific MLX indicators, return `"MLX"`.
    - Third, if still uncertain, use `HfApi().model_info(repo_id)` to check for the `"gguf"` library or `"mlx"` tags.
    - Return `None` if no match is found.
3.  **Update `manage_models`**:
    - Call the new helper function for each model found.
    - Only add models to `found_models` if the helper returns a valid type (`MLX` or `GGUF`).
    - Update the display logic to show the correct type.
4.  **Error Handling**: Wrap the Hub API call in a try-except block to handle cases where the model might be private or the network is unavailable.

# hf-lmstudio

A [`hf` CLI extension](https://huggingface.co/docs/huggingface_hub/main/en/guides/cli-extensions) that links models in the hf cache to the LM Studio/Bionic downloads folder.

This allows LM Studio to share/use models that you previously downloaded with hf or other hf-compatible tools.

## Install

Install the extension straight from GitHub:

```shell
hf extensions install jon-hedgerows/hf-lmstudiosync
```

That's it. The `hf lmstudiosync` command is now available alongside the built-in `hf` commands.

## Usage

Select and link models interactively:
`hf lmstudiosync`

Link all MLX and GGUF models:
`hf lmstudiosync --all`

### Requirements

- The `hf` CLI from `huggingface_hub` >= 1.23 (On MacOS `brew install hf`).

## Acknowledgements

Based on an original idea by Ivan Fioravanti [lmstudio_hf](https://github.com/ivanfioravanti/lmstudio_hf)

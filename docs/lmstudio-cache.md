# LM Studio's expected models directory structure

LM Studio aims to preserves the directory structure of models downloaded from Hugging Face. The expected directory structure is as follows:

```text
~/.lmstudio/models/
└── publisher/
    └── model/
        └── model-file.gguf
```

For example, if you have a model named `ocelot-v1` published by `infra-ai`, the structure would look like this:

```text
~/.lmstudio/models/
└── infra-ai/
    └── ocelot-v1/
        └── ocelot-v1-instruct-q4_0.gguf
```

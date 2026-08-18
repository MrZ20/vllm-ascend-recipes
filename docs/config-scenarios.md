# Recipe 基线与可配置参数

## 目标

1. **验证 tutorial 准确性**:每个基线场景(单机 A2 / 单机 A3 / 多机 PD 分离)的 serve 参数以
   tutorial 原始配置为**默认值**,CI 用默认值跑,保证"教程怎么写,CI 就怎么跑"。
2. **特殊参数可配置**:文档里的 Key Parameter Descriptions(`--max-model-len`、
   `--no-enable-prefix-caching`、`--max-num-seqs`、`--gpu-memory-utilization` 等)
   抽成 `config_params`,给出默认值(取自 tutorial),页面/CI 可覆盖。
3. **上游字段对齐**:顶层字段(`meta/model/variants/compatible_strategies/features/
dependencies`)与 vllm-project/recipes schema 一致;`scenarios` 保留为我们的扩展字段，
   其 serve 命令参数用占位符渲染。
4. **执行边界**:当前多节点 Runtime 执行已经展开的 plan,不从 Recipe 字段推断拓扑；
   后续由独立 Converter 将结构化 Recipe 转换为 plan。

## 字段模型

```yaml
meta: # 上游对齐(含 related_recipes 等)
model: # 上游对齐(含 base_args/base_env/install/nightly_required)
variants: # 上游:精度/显存/描述(默认 = tutorial)
compatible_strategies: # 上游:部署策略
features: # 上游:模型特性(expert_parallel/tool_calling/reasoning/spec_decoding)
opt_in_features: []
dependencies: # 上游:额外 pip 依赖

# —— 新增:可配置参数(默认值 = tutorial 原始配置)——
config_params:
  max_model_len:
    { default: 1048576, type: number, description: '最大上下文长度(输入+输出),按实际场景调整' }
  max_num_seqs: { default: 256, type: number, description: '每个 DP 组最大并发序列数' }
  gpu_memory_utilization: { default: 0.90, type: number, description: 'KV cache 可用显存比例' }
  prefix_caching:
    { default: false, type: bool, description: '默认关闭;开启后去掉 --no-enable-prefix-caching' }

scenarios: # 页面展示与 Recipe 场景基线
  - npu: Atlas 800I A2
    deployment: 单节点-多卡
    steps:
      content: |
        vllm serve ... --max-model-len {{max_model_len}} \
          --max-num-seqs {{max_num_seqs}} \
          --gpu-memory-utilization {{gpu_memory_utilization}} \
          {{prefix_caching:--no-enable-prefix-caching}} ...
  - npu: Atlas 800I A3
    deployment: 多节点-PD分离
```

## 占位符约定

- `{{name}}` — 用 `config_params.name.default`(或用户覆盖值)替换。
- `{{name:text}}` — 布尔参数:name 为 falsy 时渲染 `text`,为 truthy 时渲染空。
  (示例:`{{prefix_caching:--no-enable-prefix-caching}}` → 默认 false 时输出
  `--no-enable-prefix-caching`,勾选开启后该参数消失。)

## 渲染与执行

- **页面**:CascadeSelector 增加"参数配置"面板,显示 config_params(默认值),输入后实时
  替换到步骤的 serve 命令。
- **多节点 CI**:当前 workflow 执行 `test/recipe/multi_node/configs/` 下的可执行 plan；
  Recipe YAML 到 plan 的转换将在 Converter 落地后接入。

## 可提取脚本（迁移中）

需要由后续 Converter 提取的完整脚本放在场景的 `scripts` 映射中，并在正文使用
`{{script:name}}`。页面将其渲染为 `language` 指定语言的代码块。普通说明命令继续直接写在
`steps[].content` 中，不为“可能有用”而拆字段。脚本键名不定义业务白名单，只要求正文引用
能以 `{{script:...}}` 完整、精确地指向对应键。

节点相关脚本使用零基角色编号，例如 `prefill-0-template`、`prefill-1-template`、
`decode-0-launch`；这样同一角色扩展到多个节点时仍能唯一定位脚本。

面向整个拓扑的脚本不加节点编号，例如端到端服务验证使用 `service-check`。它与节点启动脚本
一起由正文嵌入，供后续 Converter 分别生成服务和检查阶段。

采用该结构的场景使用固定拓扑值：`deployment` 仅为 `pd` / `non-pd`；case 分别写成
`1p1d`、`2p1d` 或 `1-node`、`2-node` 等机器可读格式。页面直接在 PD case 选项原有的
悬停内容中解释 P/D。启动端口保留在脚本正文，不额外维护重复配置。

当前固定两组中英文模板，作为未来 Converter 的输入基线：

| 类型 | Recipe 场景 | 现有中间态 |
| --- | --- | --- |
| PD external DP | `DeepSeek-V2-Lite-W8A8.yaml` 的 `pd / 1p1d` | `deepseek-v2-lite-pd-2n2c/plan.yaml` |
| 非 PD internal DP | `Qwen3-30B-A3B.yaml` 的 `non-pd / 2-node` | `qwen3-30b-a3b-dp-2n2c/plan.yaml` |

目前仅校验 Recipe 模板与既有 plan 的关键拓扑契约，没有实现 Recipe 到 plan 的转换。

## 兼容性

- 上游字段与我们的教程字段共存;上游 build 忽略未知字段(已实测)。
- 现有 `%%CONFIG:key%%` 机制保留(用于步骤内启停文本),新占位符机制并行。

# 固定框架内的 GUI Agent 实现

本次改造只替换 `agent.py`，新增 `utils/gui_agent/`、静态技能 JSON 和离线回归测试。`agent_base.py`、`device_controller.py`、`requirements.txt`、`run_tests.py`、`test_runner.py` 保持原始内容。没有新增第三方依赖，没有离线技能迭代或在线自进化。

## 调用链路

每次 `act()`：建立当前截图观察 → 读取任务约束和待核验动作 → 按需选择静态 Skill → 主模型核验上一动作并提出下一动作 → 更新证据账本 → 规则与完成门禁 → 必要时同模型 Critic → 返回原 `AgentOutput`。

主模型与 Critic 均调用继承的 `_call_api()`，模型、地址、Key 读取与底层超时沿用基类。Critic 使用独立消息上下文。全部已收到响应的 usage（包含空回复和格式修复）汇总到当前 `AgentOutput.usage`。

设备操作由原框架完成，Agent 不执行 ADB、不主动获取截图，也收不到设备返回码。实际操作记为 pending；下一次传入的截图提供效果证据后，才计入完成状态。WAIT 不抹掉之前尚待核验的实际操作。

## 文件分工

| 文件 | 职责 |
|---|---|
| agent.py | 基类适配、模型调用、重试与预算、Critic、标准输出 |
| contracts.py / decoder.py | 内部结构化决策与安全的旧 Action 格式兼容 |
| task.py | App 别名、初始成功条件、条目数量与评分约束 |
| observer.py | 框架截图的步骤标识与轻量变化指纹 |
| ledger.py | 事实、条目、条件、历史及待核验动作 |
| skills.py / gui_agent_skills.json | 人工维护的静态技能与 App 过滤 |
| prompt.py | 主模型和独立 Critic 上下文 |
| guards.py | 参数后的业务检查、副作用、重复动作与完成门禁 |

解析使用标准库 `json` 和安全 AST 字面量读取，不执行模型生成的代码。无法解析时最多一次格式修复，再返回合法 WAIT，不返回空动作或假 COMPLETE。JSON 决策必须引用当前 observation_id；旧 Action 格式仅做动作兼容，无法绕过完成证据检查。

## 状态和完成

任务内记忆区分 page 与 task 事实，page 事实跨步骤失效，task 产物供跨 App 使用。事实带 step、observation_id、证据与视觉核验来源；旧值被替代时保留有界历史事件。视觉来源不等于程序能够证明截图内容正确，最终任务正确性仍需真机评估。

条目采用稳定 id 和规范化名称去重。原始评分条件由程序比较；例如 4.8 不满足“超过 4.8”。对应添加动作未经下一截图核验，不计入 added_count。目标歌单数量与条目数量分开提取，例如“榜单前五放进一个歌单”的 target_count 为 5。

完成需要全部必需条件有证据、当前 task_goal 核验、实际动作无未决状态、数量及评分条件通过，再由 Critic 确认全部条件。Critic 不可用、格式错误、引用旧截图、条件缺失或拒绝时返回 WAIT。

`reset()` 清空任务、账本、历史、待核验动作、技能上下文和预算；不同任务或步骤重启也会重置，防止串题。

## 运行和配置

沿用原命令，例如 `python run_tests.py`。它会控制真机并执行原任务集，测试前需按原项目要求连接手机和设置模型 Key。本次开发验证只使用模拟模型与截图，没有调用付费 API 或执行手机任务。

新增测试命令：

```powershell
python -m unittest discover -s tests -v
```

可通过原 `Agent(config)` 入口传入 Agent 自有参数，不改基类或 runner：

```python
from agent import Agent

agent = Agent({
    "history_window": 12,
    "retry_count": 3,
    "max_output_tokens": 2200,
    "max_api_calls": 240,
    "max_task_tokens": 250000,
    "max_critic_calls": 30,
})
```

技能文件位于 `utils/gui_agent_skills.json`，通过绝对模块路径加载，工作目录变化不会影响定位。每项包含 id、category、apps、keywords 与 instructions，默认 active；只注入相关的少量技能。没有自动学习、候选生成或发布过程。

默认副作用范围不含支付、真实下单、删除和敏感授权。明确要求发布评论的任务允许对应发布动作，仍接受必要视觉审核。其他副作用须在集成侧通过 allowed_side_effects 明确配置；这不是新增交互确认界面。截图中未被正确识别的按钮仍可能被视觉模型误判，不能声称规则覆盖了所有业务风险。

## 固定框架造成的限制

- 无独立失败终止动作：内部不可继续时记录原因并返回 WAIT，由原框架按原步数结束，不把失败包装成 COMPLETE。
- 无交互式澄清和设备错误码回传通道。
- 无新增 UI Tree、独立 OCR、设备应用查询或动态启动器。
- 基类固定请求超时未调整；预算仅阻止后续调用，不能取消在途请求。
- 规则、接口和状态流的离线测试不代表真实模型或真机成功率已验证。

五个固定文件的原始 SHA-256 保存在回归测试中，用于持续检查实施边界。

本次验证结果：47 项离线回归测试通过，改动文件的 Python 编译检查通过，原 `run_tests.py --help` 入口通过。未运行真实模型或手机任务。

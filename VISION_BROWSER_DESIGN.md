# 视觉增强浏览器操作设计方案

## 1. 文档目的

本文档将视觉能力接入当前 Agent 浏览器操作链路，目标是在不牺牲 DOM 自动化稳定性的前提下，让 Agent 能可靠处理图标按钮、图片控件、复杂布局、Canvas、视觉弹窗和 DOM 描述不清的网页。

核心原则：

> 让 DOM 负责精确，让视觉负责理解，让验证层负责判断，让 Agent 负责规划。

本文档是设计建议，不直接改变现有实现。

## 2. 当前项目能力

当前项目已经具备较完整的浏览器和视觉基础设施：

- `loop_agent_core/browse.py`
  - 通过 Playwright 控制浏览器。
  - 支持打开、点击、输入、回车、按键、悬停、下拉、滚动、等待、后退、截图和关闭。
  - 返回 URL、标题、页面文本和交互元素清单。
  - 支持编号、`css=` 和 `xpath=` 目标。
  - 使用专用浏览器线程，避免 Playwright 跨线程问题。
  - 支持 iframe、持久化 profile、Cookie、localStorage、sessionStorage 和 SPA 状态恢复。
- `loop_agent_core/vision.py`
  - 通过本地 `llama.cpp` 的 OpenAI 兼容接口调用 Qwen VLM。
  - 支持分析本地截图、图表、照片和 UI 图片。
- `loop_agent_core/visual_locate.py`
  - 截取浏览器页面截图。
  - 让 VLM 返回目标区域包围框。
  - 将包围框吸附到真实 DOM 元素。
  - 返回元素编号，供 `browser_act` 执行点击或输入。
- `loop_agent_core/validation.py`
  - 对工具结果进行基础有效性检查。
  - 已能识别 `browser_act`、`visual_locate` 等工具的明显错误。
- `loop_agent_core/prompts.py`
  - 已规定 DOM 优先、视觉兜底的使用策略。

当前实现的基础方向是正确的：不把所有动作交给视觉模型，而是优先使用可验证、可复现的 DOM 定位。

## 3. 现状问题

### 3.1 动作成功与页面状态成功尚未完全分离

当前点击流程主要是：

```text
定位元素 -> Playwright click -> 等待一小段时间 -> 返回 OK
```

Playwright 点击成功，只能证明输入事件已发送，不能证明业务动作成功。例如：

- 点击了错误的相似按钮。
- 元素被遮挡或页面状态没有更新。
- 菜单没有打开。
- 表单提交失败但没有抛异常。
- 收藏、点赞、关注状态没有改变。
- SPA URL 不变，但页面内容发生了异步变化。

需要增加动作后的状态验证。

### 3.2 元素编号是短期引用，不是稳定身份

交互元素清单中的编号依赖当前扫描顺序。页面滚动、异步渲染、弹窗出现或菜单展开后，编号可能变化。

因此：

- 编号适合紧邻的下一步操作。
- 编号不适合跨多个动作长期保存。
- 视觉定位结果也不应只返回一个裸编号。

建议增加稳定的 `element_id`，编号只作为当前 observation 的便捷别名。

### 3.3 截图坐标与浏览器坐标需要显式映射

`visual_locate.py` 当前默认 VLM 返回的截图坐标可以直接与 Playwright 元素坐标比较。多数普通环境可行，但 Windows 缩放、DPI、截图缩放、iframe、滚动和页面 transform 都可能造成偏差。

需要明确区分：

```text
截图坐标 -> viewport 坐标 -> iframe 内坐标 -> DOM 元素坐标
```

### 3.4 视觉定位缺少候选排序和置信度

复杂页面可能有多个相似目标。单个包围框加最近距离吸附，有时会选中错误候选。

视觉模型应返回候选框、置信度和简短理由；代码再结合 DOM 文本、ARIA、位置、可见性和 enabled 状态重新评分。

### 3.5 浏览器 observation 尚未统一

截图、DOM 清单和页面文本目前由不同流程分别生成。若它们不是同一时刻产生，可能出现：

- 截图中有按钮，但 DOM 清单尚未更新。
- DOM 中有元素，但截图已经是旧页面。
- 视觉定位和编号定位引用不同页面状态。

需要统一的浏览器观察对象。

## 4. 目标架构

推荐将浏览器控制升级为以下闭环：

```text
用户目标
   |
   v
Agent 规划动作
   |
   v
Browser Perception
   +-- DOM 结构
   +-- 页面文本
   +-- 截图
   +-- 可访问性信息
   +-- 视觉模型
   |
   v
Action Resolver
   +-- CSS / XPath
   +-- 稳定 element_id
   +-- DOM 文本和 ARIA 匹配
   +-- VLM 包围框吸附
   +-- 纯视觉坐标定位
   |
   v
Playwright 执行动作
   |
   v
Action Verifier
   +-- URL 是否变化
   +-- DOM 是否变化
   +-- 元素属性是否变化
   +-- 页面文本是否变化
   +-- 截图差异
   +-- VLM 结果判断
   |
   +--> 成功：继续任务
   +--> 失败：重新观察并重规划
   +--> 不确定：重试或请求用户确认
```

## 5. 统一浏览器观察对象

建议新增 `loop_agent_core/browser_observation.py`，定义统一的观察结果。实现上可以使用 `dataclass` 或结构化字典。

推荐字段：

```python
BrowserObservation(
    profile="default",
    url="https://example.com",
    title="Example",
    viewport={"width": 1366, "height": 650},
    scroll={"x": 0, "y": 780},
    screenshot_path="screenshots/page_20260814_120000.png",
    screenshot_hash="...",
    page_text="...",
    elements=[...],
    timestamp="2026-08-14T12:00:00+08:00",
)
```

每个元素建议包含：

```json
{
  "element_id": "element:8f31a2",
  "index": 8,
  "frame_index": 0,
  "tag": "button",
  "role": "button",
  "label": "设置",
  "aria_label": "设置",
  "title": "",
  "css": "button.settings",
  "box": {"x": 1200, "y": 20, "width": 36, "height": 36},
  "visible": true,
  "enabled": true,
  "covered": false
}
```

设计要求：

1. 截图、DOM、页面文本和滚动位置尽量在同一观察周期内采集。
2. 所有元素编号只在该 observation 内有效。
3. `element_id` 用于跨工具调用重新匹配。
4. observation 应包含时间戳和页面标识，防止旧结果误用于新页面。
5. 可以保留完整 observation 到调试目录，便于复现失败。

## 6. 三种浏览器操作模式

### 6.1 DOM 快速模式

适用于有清晰文本、ARIA 或稳定选择器的目标。

```text
DOM observation
 -> 文本 / ARIA / CSS 匹配
 -> Playwright 操作
 -> DOM 或 URL 验证
```

这是默认模式，速度快、成本低、可重复性最好。

### 6.2 视觉定位模式

适用于图标、图片按钮、无文本 div、复杂布局和视觉关系目标。

```text
DOM observation + screenshot
 -> VLM 返回目标候选框
 -> 坐标归一化
 -> 吸附到 DOM 候选
 -> Playwright 操作
 -> 状态验证
```

这是当前 `visual_locate` 最适合演进的方向。

### 6.3 纯视觉坐标模式

适用于 DOM 无法访问或无法表达目标的页面，例如：

- Canvas。
- WebGL。
- 图片编辑器。
- 远程桌面页面。
- 部分跨域 iframe。
- 浏览器插件界面。
- 游戏和图形化控件。

建议新增独立工具，而不是把它隐含在 `browser_act(click)` 中：

```text
visual_click
visual_drag
visual_type
```

纯视觉操作必须有更严格的坐标校验、截图验证和高风险确认。

## 7. `visual_locate` 改进方案

### 7.1 使用结构化 JSON 输出

当前实现依赖正则解析 VLM 输出的四个数字。应优先要求模型返回 JSON，同时保留数字正则作为兼容 fallback。

推荐格式：

```json
{
  "image_width": 1366,
  "image_height": 650,
  "boxes": [
    {
      "x1": 520,
      "y1": 60,
      "x2": 640,
      "y2": 90,
      "confidence": 0.88,
      "label": "搜索框",
      "reason": "位于页面顶部工具栏区域"
    }
  ]
}
```

### 7.2 返回多个候选

对于“更多按钮”“右侧设置”“第一个商品”等目标，返回多个候选：

```json
{
  "target": "右上角设置按钮",
  "candidates": [
    {
      "box": [1200, 20, 1235, 55],
      "confidence": 0.91,
      "label": "设置"
    },
    {
      "box": [1160, 20, 1195, 55],
      "confidence": 0.34,
      "label": "用户菜单"
    }
  ]
}
```

### 7.3 统一坐标协议

所有视觉结果都应携带图片宽高，并明确坐标属于：

- screenshot viewport；还是
- full-page screenshot；还是
- iframe 内截图。

代码负责将视觉坐标映射到 Playwright 的 viewport 坐标，不能把映射责任交给 Agent。

### 7.4 视觉与 DOM 联合打分

推荐评分因素：

```text
总分 =
  视觉置信度 * 0.35
+ 包围框交集比例 * 0.25
+ 中心点距离得分 * 0.15
+ 文本 / ARIA 匹配 * 0.15
+ 可见且 enabled * 0.10
```

具体权重可以通过测试集调整。

### 7.5 低置信度不自动点击

建议设置阈值：

- `>= 0.80`：可自动执行普通低风险动作。
- `0.60 ~ 0.80`：返回候选，让 Agent 结合 DOM 和目标上下文二次判断。
- `< 0.60`：重新截图、调整提示词或请求用户确认。

涉及支付、删除、发送、发布、购买、权限变更时，应使用更高阈值并经过确认门。

## 8. 动作后验证闭环

### 8.1 每个动作保存 before / after observation

推荐流程：

```text
获取 before observation
 -> 执行动作
 -> 等待页面稳定
 -> 获取 after observation
 -> 计算变化信号
 -> 判断成功 / 失败 / 不确定
```

### 8.2 非视觉验证优先

按成本从低到高依次验证：

1. URL 是否改变。
2. 页面标题是否改变。
3. 目标元素是否消失、出现或属性改变。
4. 页面文本是否出现预期内容。
5. DOM 元素数量和结构是否改变。
6. 截图哈希或区域差异是否改变。
7. 最后才调用 VLM 判断视觉结果。

### 8.3 验证结果格式

建议 `browser_act` 返回结构化结果：

```json
{
  "action": {
    "type": "click",
    "target": "element:8f31a2"
  },
  "execution": {
    "status": "performed",
    "clicked_box": [1200, 20, 1235, 55]
  },
  "verification": {
    "status": "success",
    "confidence": 0.94,
    "signals": [
      "settings menu appeared",
      "interactive element set changed"
    ]
  }
}
```

`verification.status` 建议支持：

```text
success
failed
uncertain
not_checked
```

### 8.4 失败重规划

动作验证失败时，Agent 不应立即重复相同动作。建议：

1. 重新获取 observation。
2. 判断页面是否已经变化但结果未被识别。
3. 检查目标是否仍然存在。
4. 尝试新的定位策略。
5. 最多重试两次。
6. 连续两次 observation 无变化时判定页面可能卡住。
7. 高风险动作转入用户确认。

## 9. 建议新增工具

### 9.1 `observe_browser`

只观察，不执行动作：

```json
{
  "profile": "default",
  "include_screenshot": true,
  "include_dom": true,
  "include_text": true,
  "focus": "页面右上角的设置按钮"
}
```

用途：

- 在复杂任务开始前获取统一状态。
- 视觉定位前复用同一份截图和 DOM。
- 调试页面状态。
- 避免使用 `browser_act(action="screenshot")` 兼作观察工具。

### 9.2 `browser_act` 增加验证参数

推荐增加：

```json
{
  "action": "click",
  "target": "element:8f31a2",
  "verify": "auto",
  "expected": "出现设置菜单"
}
```

`verify` 可支持：

```text
none
dom
url
screenshot
vision
auto
```

### 9.3 `visual_click`

适用于 DOM 不可操作区域：

```json
{
  "target": "点击画布中的播放按钮",
  "confidence_threshold": 0.75
}
```

内部流程：

```text
截图
 -> VLM 定位
 -> 坐标合法性检查
 -> 安全确认
 -> mouse.click
 -> 截图和状态验证
```

### 9.4 `browser_task`

在单步 `browser_act` 之上提供受限的高层任务工具：

```json
{
  "goal": "打开网页，搜索 Qwen3.5，并打开第一个结果",
  "max_steps": 8
}
```

内部每一步仍必须遵循：

```text
observe -> resolve -> act -> verify
```

高层工具不能绕过单步日志、安全确认和动作验证。

## 10. 稳定元素身份设计

元素编号仍然可以保留，但建议增加稳定 ID。ID 可以由以下信息计算哈希：

```text
规范化 URL
+ frame index
+ tag
+ role
+ aria-label
+ 文本摘要
+ CSS 路径
```

示例：

```text
element:8f31a2
```

重新定位时按以下顺序尝试：

1. 原 frame 和 CSS 路径。
2. `element_id` 对应的文本、ARIA 和 role。
3. 页面内唯一文本或 label。
4. 视觉包围框吸附。
5. 重新生成 observation 并交给 Agent 选择。

如果目标页面发生导航，旧 `element_id` 必须失效，不能静默复用。

## 11. Agent 决策规则

建议将以下规则加入 `loop_agent_core/prompts.py`：

```text
1. 先观察页面，不要根据过期元素清单猜测。
2. DOM 有唯一且清晰的目标时，优先使用 DOM。
3. 有多个 DOM 候选时，结合文本、ARIA、位置和页面上下文排序。
4. 图标、图片或视觉区域目标使用 visual_locate。
5. DOM 吸附失败时，才使用纯坐标视觉操作。
6. 每个动作后检查 verification.status。
7. 验证失败时重新观察，不要盲目重复相同动作。
8. 连续两次无状态变化时，判断为卡住并更换策略。
9. 删除、购买、支付、发送、发布和权限变更必须请求用户确认。
10. 不尝试自动绕过验证码；遇到验证码时暂停并交给用户。
```

## 12. 安全策略

视觉能力会扩大 Agent 的操作范围，因此安全边界必须前置：

- 纯视觉坐标点击必须限制在当前 viewport 内。
- 点击前检查坐标是否落在 VLM 返回区域内。
- 高风险动作必须经过确认门。
- 不允许视觉模型直接执行任意 JavaScript。
- 不允许视觉模型绕过 URL、域名和网络访问限制。
- 记录截图、目标、坐标、置信度、动作和验证结果。
- profile、Cookie 和本地会话仍由现有 profile 机制管理。
- 验证码和安全挑战交给用户，不做自动绕过。
- 视觉结果不应被视为事实，必须结合页面状态和操作结果验证。

## 13. 分阶段实施计划

### 阶段一：动作后验证

修改重点：

- `loop_agent_core/browse.py`
- `loop_agent_core/validation.py`
- 相关浏览器测试

交付内容：

- 保存动作前后的 URL、文本和元素摘要。
- 对点击、输入、选择和回车增加基础验证。
- 返回 `success`、`failed` 或 `uncertain`。
- 验证失败时向 Agent 注入可操作的重试建议。

这是收益最高、风险最低的一步。

### 阶段二：统一 observation

新增：

- `loop_agent_core/browser_observation.py`
- `observe_browser` 工具

修改：

- `browse.py`
- `visual_locate.py`
- `schemas.py`

交付内容：

- 截图、DOM、文本、滚动位置统一采集。
- `visual_locate` 复用 observation。
- 加入截图哈希和页面状态标识。

### 阶段三：稳定 element_id 和结构化视觉输出

修改：

- `browse.py`
- `visual_locate.py`
- `schemas.py`

交付内容：

- 元素增加稳定身份。
- VLM 返回 JSON 候选框和置信度。
- 统一 screenshot / viewport / iframe 坐标映射。
- 保留现有编号和数字正则作为兼容路径。

### 阶段四：纯视觉操作

新增：

- `visual_click`
- `visual_drag`
- 必要时增加 `visual_type`

交付内容：

- 支持 Canvas、WebGL 和 DOM 不可用控件。
- 动作前后截图验证。
- 严格置信度阈值和确认门。

### 阶段五：高层浏览器任务

新增：

- `browser_task`
- 任务级状态机和最大步数保护。

交付内容：

- 让 Agent 能执行多步网页目标。
- 每一步仍走统一 observation、动作解析和验证。
- 失败后重新规划，不盲目重复。

## 14. 测试策略

### 单元测试

建议覆盖：

- 视觉 JSON 解析。
- 坐标归一化和 viewport 映射。
- iframe 坐标转换。
- 元素候选联合评分。
- 稳定 `element_id` 生成和失效。
- 截图区域变化检测。
- 验证状态判定。
- 低置信度处理。

### 浏览器集成测试

准备一个本地测试页面，覆盖：

- 普通文本按钮。
- 只有图标的按钮。
- 动态菜单。
- 异步加载内容。
- 表单校验错误。
- 相似按钮列表。
- iframe 控件。
- Canvas 控件。
- 滚动后元素重新排序。
- 页面无 DOM 交互元素但有视觉控件。

### 回归指标

建议记录：

- 目标定位成功率。
- 首次动作成功率。
- 动作后验证准确率。
- 平均每步延迟。
- VLM 调用次数。
- 视觉 fallback 比例。
- 错误点击率。
- 重试次数。
- 高风险动作误执行率。

目标不是让每个动作都使用视觉，而是让视觉只在需要时介入，并且介入后可验证。

## 15. 推荐优先级

按投入产出比排序：

1. **动作后验证**：先解决“点击了但不知道是否成功”。
2. **统一 observation**：解决截图、DOM 和页面文本不同步。
3. **稳定 element_id**：降低动态页面中编号失效问题。
4. **结构化视觉输出和坐标映射**：提高视觉定位可靠性。
5. **纯视觉操作**：覆盖 Canvas、WebGL 等特殊页面。
6. **高层 browser_task**：最后再封装多步任务，避免把不稳定的底层能力放大。

## 16. 总结

当前项目不需要被改造成纯视觉浏览器。最合理的方向是构建一个混合浏览器 Agent：

```text
DOM：精确定位和执行
截图：提供页面整体上下文
VLM：理解视觉目标和复杂布局
Playwright：执行浏览器动作
验证器：确认动作产生了预期结果
Agent：规划下一步和处理失败
```

最终可靠闭环是：

```text
观察 -> 解析 -> 操作 -> 验证 -> 重规划
```

其中最应该优先实现的是动作后验证，而不是继续单独增强视觉定位。只要验证闭环建立起来，即使视觉模型偶尔定位不准，Agent 也能及时发现、重新观察并换策略，而不是把错误动作继续累积下去。

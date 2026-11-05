# 按分支、叫牌局面与手牌阅读体系

## 1. 一份执行定义，多种上下文视图

执行与发布仍使用完整的v1/v2 IR，体系内容哈希、规则优先级、公开协议和叫牌行为不变。导航层从这份定义自动生成目录与依赖索引，不维护第二份手写叫品含义，也不要求研究者先翻完全部规则。

研究时默认读取：

1. 相关决策规则、公开声明、状态转移及后续阶段；
2. 真正引用到的共享特征和参数；
3. 入口/出口、公共上下文、外部引用及全局优先级接口。

分支视图不是独立可执行的小体系。只读取某些分支不等于只验证这些分支；完整编译、公开保证、实际回归和晋级门禁保持不变。模块只是导航方式，不是研究权限或人类约定白名单。

## 2. 稳定节点与版本

响应使用`schema=bridge-system-navigation/v1`，每次带实际读取的`system_hash`。节点带当前版本中的源`pointer`，编辑时仍使用既有JSON Pointer原子操作；不能把旧版本数组下标直接套到新版本。

规则、声明和转移同时返回`edit_precondition`，可直接放进编辑批次先核对当前位置的ID。读取的正文是执行器正规化后的有效定义，可能包含原文省略的默认值；新增默认字段使用`add`，不要假设原文已经有该字段。

| 节点 | 示例 |
|---|---|
| 决策规则 | `rule:prec.nt.quantitative.r123` |
| 公开转移 | `transition:nt/prec.nt.reply_4NT.t26` |
| 公开声明 | `announcement:<id>` |
| 共享表达式 | `feature:<name>` |
| 参数定义 | `parameter:<name>` |
| 协议阶段 | `state:<name>` |
| 兜底 | `fallback` |

转移键的阶段名与转移ID分别按JSON Pointer转义：`~`为`~0`、`/`为`~1`。不要猜规则编号；先搜索，使用返回的键与源路径。

## 3. 统一查询

CLI：

```bash
.venv/bin/bridge-evolve system browse systems/classics-v5.5.4-compact/prec.json
.venv/bin/bridge-evolve system browse systems/classics-v5.5.4-compact/prec.json \
  --request query.json --public-ew systems/classics-v5.5.4-compact/ccba.json
```

`query.json`是以下请求之一。CLI的`--public-ns/--public-ew`显式提供静态对手定义；不填时不假设双方采用同一体系。

### 目录与定位

```json
{"view":"catalog","limit":100}
```

```json
{"view":"search","state":"nt","call":"4NT","kind":"rule","limit":20}
```

搜索支持`query`（文本）、`kind`、`module`、`state`和`call`。目录模块包括规则命名空间和协议阶段，不强迫新体系采用传统开叫分类。动态动作与私有条件保持保守候选，搜索命中不证明当前牌会选择该叫品。

### 批量定义与依赖闭包

```json
{"view":"read","keys":["transition:nt/prec.nt.reply_4NT.t26"],"dependencies":true,"limit":20}
```

响应包含源定义、可读条件预览、递归特征/参数引用、外部使用者计数和阶段接口。不把大量重复表达式全文内联。`context_fields`单列对可信公开上下文原语的依赖；这些原语的计算仍以执行引擎为准。

只研究某项赋值时可缩小到节点内的相对路径：

```json
{"view":"read","keys":["transition:nt/prec.nt.reply_4NT.t26"],"sections":["/when","/set/nt_slam_invited"]}
```

`sections`中的路径必须实际存在。依赖闭包只针对选中部分，不把其他赋值的引用混进来。返回的是研究视图，不冒称覆盖其他公开转移。

页面按条数和字节上限渐进返回；`next_offset`指向下一页，`dependencies.complete_in_response`明确说明当前响应是否包含全部所选显式依赖。不能把第一页当成完整闭包。单个超大节点会保留描述和精确源路径，明确标记正文未返回。

### 共享影响范围与语义版本差异

```json
{"view":"references","key":"feature:hcp","transitive":true}
```

```json
{"view":"diff","kind":"transition"}
```

CLI比较时加`--compare-to <父版本JSON>`；研究工具使用`compare_to="parent"`。差异通过递归引用的实际表达式语义判断，不因规则重排、压缩或生成特征名变化就把整套体系报告成修改。默认不铺开`shared.*`生成节点的增删噪声；可用`kind="feature"`显式查看。

### 在叫牌序列中查看某个叫品

```json
{"view":"auction","dealer":"N","vulnerability":"none","history":["1NT","P"],"call":"4NT"}
```

也可传完整序列并用**从1开始的**`turn`指向其中一个叫品：

```json
{"view":"auction","dealer":"N","history":["1NT","P","4NT","P","P","P"],"turn":3}
```

只用被指向叫品之前的历史解释它，不拿后续叫牌反推原意。查询体系应属于该叫品的行动方；显式披露的本方定义与之冲突时拒绝查询。

结果包含当前阶段、公开声明、实际公共转移、公开信息前后变化及对应源键。可能的发送规则另列，绝不从某条私有规则或实际持牌推导公共保证。人工叫品、逼叫义务、非法序列和缺少必要对手定义仍按原引擎检查，不补猜含义或替换为Pass。

### 给定本人手牌判断下一叫

```json
{"view":"hand","observation":{"hand":"AKQ.JT9.KQ32.876","seat":"N","dealer":"N","vulnerability":"none","history":[]}}
```

调用原有`decide`，返回真实叫品、命中规则键及上下文入口；不把整份体系展开到`public_meaning`，不需要其他三手牌、DDS或模型调用。`decision`标为私有诊断，独立于只依赖公共协议的`public_meaning`。

## 4. 研究Agent与展示入口

研究者优先使用`navigate_system(request=...,snapshot="working")`；可选`compare_to`、`public_ns/public_ew`均只能指向working、parent或已登记冻结哈希。原始`read_system`保留，供未编译的中间版本和精确编辑使用，不作为默认导航入口。

启动与handoff提供本会话的规则/特征、案例、测试和发布库存，以及诊断入口，不把另一个谱系的测试误称为本工作区内容。诊断例可用`read_evidence(view="packet",pointer="/selected_groups/0/examples/0")`直接定位，不必先拉整包。

展示接口与研究者共用查询层：

- `POST /api/systems/<hash>/query`
- `POST /api/evolutions/<id>/systems/<hash>/query`

正文为`{"request":{...},"public_systems":{"EW":"<hash>"}}`，可选`compare_to`。进化体系仍核对冻结文件哈希和可见性，不修改旧发布或实验。

普通体系及进化体系弹窗默认加载小目录。支持分支搜索、叫牌含义、本人手牌决策、引用展开；完整规范仅在主动打开或下载时加载。牌例叫品解释中可直接打开“这个叫品的体系分支”，自动携带此前公开序列及双方体系，不默认发送手牌。

本次只重构上下文组织与查询，不增加并发研究、不改变预算、不自动启动付费实验。修改可信源代码后旧实验仍不能在新引擎身份下静默续跑，后续研究应显式创建并继承新实验。

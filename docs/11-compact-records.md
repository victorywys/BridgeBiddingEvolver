# 紧凑牌例记录与Agent读取

## 1. 三种用途，不再共用一份巨大JSON

| 用途 | 内容 | 推荐入口 |
| --- | --- | --- |
| 长期保存、完整复验 | 全部原始信息，无损共享字典＋数组＋公开状态增量 | 紧凑JSONL |
| Agent先了解整批 | 牌数、叫品数、定约分布、常见规则和错误 | `records stats` |
| 分析具体问题 | 单副摘要，或指定桌次/叫品的完整上下文 | `records show` |

没有把保存格式直接当作模型提示。Agent不必阅读整个共享字典和一万多条状态变化：先取汇总，选择问题牌，再展开所需的一叫。

旧审计、输入、体系定义和报告不自动重写。新审计及新的正式评测使用紧凑记录；所有读取器仍识别旧JSON/JSONL。展示器保持原来展开后的数据结构，叫品、公开信息、私有trace、DDS、分数和错误不变。

## 2. 实测大小

对同一份1000副、2000桌、18,502次叫牌的原记录逐项往返比较：

| 表示 | 大小 |
| --- | ---: |
| 原始完整JSONL | 144,983,883字节，约145MB |
| 新格式，完整无损 | 13,648,154字节，约13.65MB |
| 新格式再gzip | 2,092,836字节，约2.09MB |
| 全部牌例的Agent摘要 | 2,459,674字节，约2.46MB |
| 默认整批汇总 | 1,197字节 |
| 示例单副摘要 | 2,234字节 |
| 示例指定一叫的完整信息 | 6,864字节 |

完整存储减少约90.6%，约为原来的1/10.6；摘要另行减少展开内容，不冒充完整证据。字节大小不等于模型token数量。全部455项Python回归、22项前端测试及实际新审计/浏览器验证通过，见[机器验证记录](../results/compact-record-validation/validation.json)。

原记录的叫牌前/后公开状态各约50MB，占主要空间。新格式不重复写整份状态，只记录同桌、同搭档方自上次快照以来的变化。静态键名、对象列布局、长文本、规则标识和特征表达式分别复用。

可直接使用[完整紧凑样例](../results/compact-record-validation/boards.compact.jsonl)、[Agent摘要](../results/compact-record-validation/boards.agent.jsonl)及[整批汇总](../results/compact-record-validation/statistics.json)。它们是旧数据的表示转换，不是重新叫牌产生的新成绩。

## 3. Agent命令

在项目根目录：

```bash
# 默认只显示前20组规则/错误及其余次数，适合先看全局
.venv/bin/python -B -m bridge_evolve records stats \
  --input results/compact-record-validation/boards.compact.jsonl

# 读取第55副的短摘要：手牌、点力/牌型、两桌序列、定约、IMP和DDS/par
.venv/bin/python -B -m bridge_evolve records show \
  --input results/compact-record-validation/boards.compact.jsonl \
  --board 55

# 只展开第二桌第三叫；prefix不含后面的叫品
.venv/bin/python -B -m bridge_evolve records show \
  --input results/compact-record-validation/boards.compact.jsonl \
  --board 55 --view decision --table 2 --turn 3

# 确实需要时，展开整副全部原始信息
.venv/bin/python -B -m bridge_evolve records show \
  --input results/compact-record-validation/boards.compact.jsonl \
  --board 55 --view full --pretty

# 旧文件转成新文件；已存在的输出会报错，不覆盖源文件
.venv/bin/python -B -m bridge_evolve records convert \
  --input old-boards.jsonl --output converted-boards.compact.jsonl
```

`--board`和`--turn`从1开始；`--index`从0开始。`--top 0`显示全部规则/错误，`--pretty`仅调整输出排版。摘要是`bridge-agent-board/v1`，整批汇总是`bridge-agent-statistics/v1`，指定一叫是`bridge-agent-decision/v1`。

单副摘要把四手牌、点力和牌型按N/E/S/W排列；叫牌行是`[seat, call, rule_id]`。DDS摘要按N/E/S/W四行、C/D/H/S/NT五列排列，字段名明确标注轴。缺少原始规则时保留`null`，不伪造规则。

指定一叫的视图保留本人的手牌、公开前缀和该叫品完整记录，不附其他三手或DDS。它是牌后诊断视图，包含选中叫品的后态，不是策略执行时的`Observation`。原运行策略的信息边界不变。

默认汇总只统计记录数量、实际叫品、规则、定约和已记录错误；没有把未记载检查状态的记录算作“通过”，而是另外给出`status_unrecorded_records`。这些计数不替代原审计报告或候选有效性检查。

## 4. 显示器和HTTP

显示器新增：

- 当前牌例／筛选结果的“Agent摘要”。
- 筛选结果的“无损压缩记录”，可再次完整导入。
- 任务详情里的实际记录大小和展开后大小。
- 新审计ZIP中的原始记录采用紧凑格式；导入审计ZIP时核对文件、报告和冻结体系身份。

原完整JSON和PBN导出继续保留。Agent摘要会明确拒绝当作完整审计导入，避免把省略的上下文误当成原来没有的信息。

无需先下载整批列表也可查询：

```text
GET /api/audits/<任务ID>/records
GET /api/audits/<任务ID>/records?view=summary&board=55
GET /api/audits/<任务ID>/records?view=decision&board=55&table=2&turn=3
GET /api/audits/<任务ID>/records?view=full&board=55
```

默认返回小型统计；响应附带任务和报告状态。失败任务不会因为统计中某些计数为零而变成通过。回收站和本机来源限制仍适用。

已知牌例ID时，原`GET /api/runs/<运行ID>/boards/<牌例ID>`可加`view=summary`或`view=decision&table=...&turn=...`。筛选导出接口的`format`新增`compact`、`agent`；默认完整详情接口不变。

## 5. JSONL v2格式

实现位于`src/bridge_evolve/evaluation/record_log.py`，只有标准库依赖。

### 文件头与字典

第一行为：

```json
{"schema":"bridge-record-log/v2","row":["keys+","strings+","expressions+","layouts+","record"],"nodes":["object","array","string-ref","public-delta","expression-ref"]}
```

其余每行是一条完整记录：

```text
[新增字段名, 新增长文本, 新增表达式, 新增对象布局, 编码记录]
```

四类字典均为本文件内追加、从0编号。先应用该行所有字典增量，再解码记录。对象布局是一组字段名编号，后续对象只写布局编号及按列排列的值。

例如第一条记录为`{"seat":"N","call":"1NT"}`：

```json
[["seat","call"],[],[],[[0,1]],[0,0,"N","1NT"]]
```

下一条相同布局只需要：

```json
[[],[],[],[],[0,0,"S","3NT"]]
```

### 节点

| 编码 | 含义 |
| --- | --- |
| JSON标量 | 原值；数字、布尔、null及短字符串直接保存 |
| `[0, layout_id, value, ...]` | 按共享布局还原对象 |
| `[1, value, ...]` | 数组；原始数组也始终使用这个标记，避免与其他节点混淆 |
| `[2, string_id]` | 共享长文本 |
| `[3, channel, changed_object, removed_key_ids]` | 公开快照增量 |
| `[4, expression_id]` | 共享的原始特征表达式 |

公开快照通道为`2×本记录内桌次索引＋搭档方`，NS为0、EW为1。每条记录开始时清空通道状态，不能跨牌共享公开或私有状态；字典仍可跨记录复用。删除字段与赋值`null`不同；布尔与整数也保持区别。原来由标准JSON序列化为数组的Python tuple继续还原为JSON数组。

一副单独保存的JSON采用`{"schema":"bridge-record/v2","row":[...]}`，该行自带全部字典，仍是合法的单个JSON对象。

## 6. 读取、索引与完整性

```python
from pathlib import Path
from bridge_evolve.evaluation.record_log import iter_records, select_record

for record in iter_records(Path("boards.compact.jsonl")):
    # 默认得到原来的完整dict/list结构。
    ...

record = select_record(Path("boards.compact.jsonl"), number=55)
```

不要再用`json.loads(line)`把紧凑行直接当作展开的牌例。读取器自动识别旧JSON、旧JSONL、紧凑JSONL和紧凑单副JSON。

索引模式只读取列表、过滤和规则检索所需内容，不还原全部公开快照与私有特征。显示器缓存文件字典和字节位置，选中牌例时才完整展开；并发读取不共享可变快照。CLI同样先扫描字典和轻量信息，只展开选中的记录。

不能把文件中间几行直接剪出来独立使用，因为其字典编号依赖前文。使用导出接口重新编码所选记录，会得到自包含的新文件。

原文件哈希、报告哈希、单行身份检查仍保留。字典越界、错误布局、未知版本、无效数字或非法状态删除都会显式报错，不补Pass、零分或缺省状态。压缩不重算叫品含义，也不删除未显示的字段；完整展开与旧JSON等价。

新审计报告的`records`和正式评测清单的`record_encoding`记录格式、实际／展开字节数、记录数量及字典项数量。失败、取消或保存故障仍保持原本的失败语义，不会靠压缩修正原始输出。

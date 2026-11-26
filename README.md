# 历史数据迁移抽样核对：活动与互动记录

ClawHunt #3247。迁移后给「活动 + 互动」结果加一道抽样核对闸门：
抽 30 场活动 / 100 条互动，逐条比对字段，检查健康分引用，输出核对记录与按原因分类的问题清单。

## 1. 运行（零第三方依赖，Python 3.11+）

```bash
# 迁移结果可用时
python run_check.py --activities 活动迁移后.csv --interactions 互动迁移后.csv --seed 20261126

# 迁移结果不可用时，用内置脱敏样本先行验证核对逻辑（交付要求的降级路径）
python run_check.py --mock --seed 20261126
```

输出到 `out/`：`check_records.csv`（逐条 diff）、`problems.csv`（每条记录一个问题，含原因分类）、
`health_check.txt`（健康分结论）、`summary.json`（汇总）。

## 2. 核对口径（三个坑，逐条说明）

### 2.1 逐字段比对，不是整条字符串比
`ACTIVITY_DIFF_KEYS` / `INTERACTION_DIFF_KEYS` 声明了业务语义字段白名单。迁移可能新增列或改列名，
但核对只认这些字段——新增/重命名不进核对集，避免误报。

### 2.2 抽样用固定种子、可复现
自写 32 位整数 LCG（`migration_check/checker.py::Lcg`），不用 `random.Random`（跨版本不保证稳定）。
同种子必同样本；`sample_digest` 给出确定性摘要。

### 2.3 问题必须归类，且分类可测试
每条问题必标原因分类：`字段错位 / 时间截断 / 关联错位 / 丢失 / 重复 / 其他`。
不允许"问题：无"这类无信息条目——`classify_problem` 有显式规则，且有反向用例。

## 3. 健康分引用检查
`check_health_usage(interaction_ids, health_source)` 判定：
- `source_table` 指向历史互动表 → 继续；
- `referenced_ids` 覆盖本批抽样的全部互动 → `OK`；
- 否则判定"未正确使用"，并给出缺多少条。

## 4. 定时执行方式（交付说明）
本单是一次性核对闸门（Stage iv 联调试运行阶段执行一次），**不是周期性任务**。
若客户要在每次迁移后重复执行，建议挂到 CI：迁移完成后的 job 里追加
`python run_check.py --activities ... --interactions ... --seed $CI_PIPELINE_ID`，
失败（退出码 1）即阻断上线。单次核对运行耗时 < 1 秒。

## 5. 刻意不做（范围边界）
- 不修复迁移数据本身（S2-IMP 的清洗/导入逻辑），只核对并报告；
- 不修改健康分计算逻辑，只检查它是否正确引用了历史互动；
- 不访问真实生产库，全部在测试环境（脱敏/虚构数据）内；
- 不生成活动/互动数据（用已有迁移输出或 mock）。

## 6. 测试
```bash
python -m unittest discover -s tests -t . -v
# 预期：Ran 16 tests ... OK
```

覆盖：抽样 30/100 且不重复、同种子同一样本、不同种子不同样本、digest 稳定、
逐字段比对、时间截断、关联错位、丢失记录、健康分三态（正确/非历史表/未覆盖）、
端到端 mock 检出 4 类问题与全一致通过。

# PRD - 电路板兼容表功能

## 1. 文档信息

| 项目 | 内容 |
|------|------|
| 版本 | v1.0 |
| 日期 | 2026-07-21 |
| 状态 | 待评审 |

## 2. 背景与目标

### 2.1 背景

IVD智能故障分析平台已具备故障规则管理、电机状态匹配、硬件故障案例等功能，但缺少电路板兼容性信息的管理与查询能力。现场工程师在排查硬件故障时，经常需要查阅PCBA编码与PCB编码的对应关系、底层版本兼容性等信息，目前这些信息分散在离线文档中，查阅效率低且容易出错。

### 2.2 目标

1. 提供电路板兼容性信息的持久化存储与管理能力
2. 支持管理员录入和批量导入兼容表数据
3. 支持普通用户按系列/型号查看兼容表
4. 数据按系列+型号隔离，与现有架构保持一致

### 2.3 范围

- **In Scope**：SMART系列（SMART500、SMART6500）的PCBA兼容表和底层兼容表
- **Out of Scope**：VENUS系列（后续版本扩展）

## 3. 用户角色与场景

| 角色 | 说明 | 操作权限 |
|------|------|----------|
| 管理员 | 后台管理人员 | 录入、编辑、删除、批量导入、查看 |
| 普通用户 | 现场工程师 | 仅查看 |

### 核心场景

| 场景 | 角色 | 描述 |
|------|------|------|
| SC-1 查看PCBA兼容表 | 普通用户 | 选择系列和型号，查看PCBA编码与PCB编码的对应关系及兼容性说明 |
| SC-2 查看底层兼容表 | 普通用户 | 选择系列和型号，查看底层版本（Bootloader/无Bootloader）兼容性 |
| SC-3 手动录入记录 | 管理员 | 在管理后台选择型号和表类型，逐条录入兼容表记录 |
| SC-4 批量导入 | 管理员 | 通过Excel/CSV文件批量导入兼容表数据 |
| SC-5 编辑/删除记录 | 管理员 | 在管理后台编辑或删除已有兼容表记录 |

## 4. 功能需求

### 4.1 数据模型

#### 4.1.1 PCBA兼容表（pcba_compat_{model}）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | SERIAL | 是 | 主键 |
| pcba_code | TEXT NOT NULL | 是 | PCBA编码 |
| pcb_code | TEXT | 否 | PCB编码 |
| pcb_silkscreen | TEXT | 否 | PCB丝印 |
| latest_version | TEXT | 否 | 最新版本 |
| board_name | TEXT | 否 | 电路板名称 |
| special_note | TEXT | 否 | 特殊说明 |
| pcba_version_compat | TEXT | 否 | PCBA版本兼容性 |
| compat_description | TEXT | 否 | 兼容性说明 |
| created_at | TIMESTAMP | 是 | 创建时间 |
| updated_at | TIMESTAMP | 是 | 更新时间 |

示例表名：pcba_compat_smart6500、pcba_compat_smart500

#### 4.1.2 底层兼容表（bootloader_compat_{model}）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | SERIAL | 是 | 主键 |
| board_mnemonic | TEXT NOT NULL | 是 | 板卡助记码 |
| board_name | TEXT | 否 | 电路板名称 |
| bootloader_version | TEXT | 否 | 底层版本（Bootloader） |
| bootloader_compat_note | TEXT | 否 | 底层版本（Bootloader）兼容性说明 |
| no_bootloader_version | TEXT | 否 | 底层版本（无Bootloader） |
| no_bootloader_compat_note | TEXT | 否 | 底层版本（无Bootloader）兼容性说明 |
| created_at | TIMESTAMP | 是 | 创建时间 |
| updated_at | TIMESTAMP | 是 | 更新时间 |

示例表名：bootloader_compat_smart6500、bootloader_compat_smart500

### 4.2 用户故事

#### US-1 查看兼容表

作为普通用户，我希望在前端页面选择系列和型号后查看PCBA兼容表和底层兼容表，以便快速获取电路板兼容性信息。

验收标准：
- AC-1.1：页面顶部有系列和型号选择器，选择后自动加载对应数据
- AC-1.2：页面有两个Tab，分别展示"PCBA兼容表"和"底层兼容表"
- AC-1.3：数据以表格形式展示，支持排序
- AC-1.4：数据为空时显示空状态提示
- AC-1.5：支持关键词搜索过滤

#### US-2 管理员手动录入

作为管理员，我希望在管理后台手动录入兼容表记录，以便维护最新的兼容性数据。

验收标准：
- AC-2.1：管理后台"知识库管理"页面新增"电路板兼容表"Tab
- AC-2.2：选择系列、型号、表类型（PCBA/底层）后，可逐条添加记录
- AC-2.3：必填字段校验（PCBA表的pcba_code、底层表的board_mnemonic）
- AC-2.4：添加成功后刷新列表

#### US-3 批量导入

作为管理员，我希望通过Excel/CSV文件批量导入兼容表数据，以便快速录入大量数据。

验收标准：
- AC-3.1：支持.xlsx、.xls、.csv格式文件导入
- AC-3.2：文件第一行为表头，需匹配字段名（支持中英文表头映射）
- AC-3.3：导入前预览数据，确认后写入数据库
- AC-3.4：导入结果反馈（成功N条、失败N条及原因）
- AC-3.5：重复记录（按唯一键判断）跳过或更新

#### US-4 编辑/删除记录

作为管理员，我希望编辑或删除已有的兼容表记录，以便修正错误数据或清理过期数据。

验收标准：
- AC-4.1：点击记录可编辑，修改后保存
- AC-4.2：支持单条删除和确认
- AC-4.3：操作后刷新列表

## 5. 非功能需求

| 类别 | 要求 |
|------|------|
| 性能 | 兼容表查询响应时间 < 500ms |
| 安全 | 管理接口需管理员登录鉴权；普通用户只读 |
| 兼容性 | 遵循现有动态表命名规则（{prefix}_{model.lower()}） |
| 数据量 | 单型号单表预计 < 5000条，无需分页优化 |

## 6. 交互规格

### 6.1 前端查看页面（/board-compatibility）

- 复用现有占位路由和模板
- 页面布局：header-bar（靛蓝主题 #6366f1）+ 内容区
- 内容区结构：
  1. 顶部：系列选择器 + 型号选择器（联动）
  2. Tab切换：PCBA兼容表 | 底层兼容表
  3. 搜索框：关键词过滤
  4. 数据表格：按现有 ivd-card 卡片样式

### 6.2 管理后台（/admin/rules）

- 在现有Tab栏新增"电路板兼容表"Tab
- Tab内布局：
  1. 系列+型号+表类型选择器
  2. 操作按钮：添加记录 | 批量导入
  3. 数据表格（可编辑/删除）

## 7. 数据要求

### 7.1 表头映射（导入用）

PCBA兼容表：

| 数据库字段 | 中文表头 | 英文表头 |
|------------|----------|----------|
| pcba_code | PCBA编码 | PCBA Code |
| pcb_code | PCB编码 | PCB Code |
| pcb_silkscreen | PCB丝印 | PCB Silkscreen |
| latest_version | 最新版本 | Latest Version |
| board_name | 电路板名称 | Board Name |
| special_note | 特殊说明 | Special Note |
| pcba_version_compat | PCBA版本兼容性 | PCBA Version Compat |
| compat_description | 兼容性说明 | Compat Description |

底层兼容表：

| 数据库字段 | 中文表头 | 英文表头 |
|------------|----------|----------|
| board_mnemonic | 板卡助记码 | Board Mnemonic |
| board_name | 电路板名称 | Board Name |
| bootloader_version | 底层版本（Bootloader） | Bootloader Version |
| bootloader_compat_note | 底层版本（Bootloader）兼容性说明 | Bootloader Compat Note |
| no_bootloader_version | 底层版本（无Bootloader） | No Bootloader Version |
| no_bootloader_compat_note | 底层版本（无Bootloader）兼容性说明 | No Bootloader Compat Note |

### 7.2 唯一键

- PCBA兼容表：pcba_code（同型号下唯一）
- 底层兼容表：board_mnemonic（同型号下唯一）

## 8. API设计

### 8.1 查看接口（无需鉴权）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/board-compat/pcba?series=SMART&model=SMART6500 | 获取PCBA兼容表 |
| GET | /api/board-compat/bootloader?series=SMART&model=SMART6500 | 获取底层兼容表 |
| GET | /api/board-compat/pcba?series=SMART&model=SMART6500&keyword=xxx | 搜索PCBA兼容表 |
| GET | /api/board-compat/bootloader?series=SMART&model=SMART6500&keyword=xxx | 搜索底层兼容表 |

### 8.2 管理接口（需鉴权）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/board-compat/pcba | 添加PCBA兼容记录 |
| PUT | /api/board-compat/pcba/{model}/{id} | 更新PCBA兼容记录 |
| DELETE | /api/board-compat/pcba/{model}/{id} | 删除PCBA兼容记录 |
| POST | /api/board-compat/pcba/import | 批量导入PCBA兼容表 |
| POST | /api/board-compat/bootloader | 添加底层兼容记录 |
| PUT | /api/board-compat/bootloader/{model}/{id} | 更新底层兼容记录 |
| DELETE | /api/board-compat/bootloader/{model}/{id} | 删除底层兼容记录 |
| POST | /api/board-compat/bootloader/import | 批量导入底层兼容表 |

## 9. 依赖与约束

| 依赖 | 说明 |
|------|------|
| 现有数据库 | series、models表已有数据，型号选择器联动依赖 |
| 动态表机制 | 复用 shared.resolve_table() 动态建表，与motor_status等保持一致 |
| 管理后台鉴权 | 复用现有session鉴权机制 |
| 前端框架 | Bootstrap 5.3.2 + Font Awesome 6.4.0 |
| 已有占位 | /board-compatibility 路由和模板已存在，需替换占位内容 |

## 10. 里程碑

| 阶段 | 内容 |
|------|------|
| P1 | 数据库建表 + API接口 + 管理后台录入/编辑/删除 |
| P2 | 前端查看页面（替换占位模板） |
| P3 | Excel/CSV批量导入 |
| P4 | 搜索过滤 + 优化 |

## 附录A：与现有功能对照

| 功能 | 电机状态规则 | 电路板兼容表（新） |
|------|-------------|-------------------|
| 数据隔离 | 按型号动态表 | 按型号动态表 |
| 管理方式 | 管理后台Tab | 管理后台Tab |
| 导入方式 | PDF导入 | Excel/CSV导入 |
| 用户查看 | 分析结果中引用 | 独立页面查看 |
| 主题色 | 绿色 | 靛蓝 #6366f1 |

# processor 项目文档介绍

processor 是数据接收、处理、审核和导出系统。

本文档把项目的功能、页面、图片和操作方法整理在一起。阅读时按照页面顺序向下查看，不需要在多个文档之间来回跳转。

## 目录

1. [项目整体流程](#1-项目整体流程)
2. [安装和启动](#2-安装和启动)
3. [首页](#3-首页)
4. [项目和接收数据](#4-项目和接收数据)
5. [工作流](#5-工作流)
6. [标注数据](#6-标注数据)
7. [视频查看和审核](#7-视频查看和审核)
8. [已通过和导出](#8-已通过和导出)
9. [失败处理](#9-失败处理)
10. [垃圾桶](#10-垃圾桶)
11. [常见问题](#11-常见问题)
12. [数据和程序说明](#12-数据和程序说明)
13. [代码结构与功能说明](#13-代码结构与功能说明)

## 1. 项目整体流程

系统的完整流程是：

```text
安装启动
  ↓
打开首页
  ↓
创建或选择项目
  ↓
接收数据
  ↓
选择并运行工作流
  ↓
查看视频和处理结果
  ↓
标注、检查和审核
  ↓
导出数据
```

系统当前的主要页面：

- Home：查看整体数据状态；
- Projects：查看项目和 Episode；
- Workflow：配置和运行工作流；
- Annotation：处理标注；
- Video Review：查看视频和审核数据；
- Trash：查看已删除的数据。

Users 用户系统目前仍在开发中，暂不纳入当前操作流程。

## 2. 安装和启动

### 2.1 下载并打开项目

下载项目后，打开：

```text
processor/
```

### 2.2 启动程序

Linux：

```bash
cd processor
python deploy.py
```

只检查电脑环境，不启动服务：

```bash
python deploy.py --check-only
```

使用外部 AI API 时：

```bash
python deploy.py --skip-vllm
```

Windows 用户可以运行项目中的 Windows 部署脚本。

### 2.3 打开网页

等待服务启动后，在浏览器打开管理员提供的地址。

看到登录页面，说明网页已经启动。打开：

```text
/health
```

可以检查服务是否正常。

> 图片位置：待补充“启动成功和登录页面”截图。

### 2.4 出现问题

- 页面打不开：检查后端是否启动；
- 数据不处理：检查 Worker 是否启动；
- 工作流搭建好后，上传对应的数据会自动跑工作流，如果没有自动处理检查一下工作流，或者手动出现跑一下

## 3. 首页

### 3.1 首页有什么作用？

首页用来查看所有数据的总体状态。

### 3.2 首页真实页面

![首页功能图](images/02-home-annotated.png)

图中主要区域：

1. 左侧页面菜单；
2. 顶部数据统计；
3. 项目列表；
4. 最近的数据；
5. 刷新按钮。

### 3.3 数据状态

| 状态      | 说明             |
| --------- | ---------------- |
| Reviewing | 正在检查         |
| Approved  | 已经通过         |
| Failed    | 处理失败         |
| Trash     | 已删除，可以恢复 |

### 3.4 怎么操作？

1. 登录后进入首页；
2. 查看顶部数据统计；
3. 点击项目名称查看项目；
4. 数据没有更新时，点击右上角 Refresh。

### 3.5 看到什么算正常？

数据从 Reviewing 变成 Approved，表示数据检查完成并通过。

如果一直显示 Processing，等待后刷新页面；仍然不变时检查 Worker。

## 4. 项目和接收数据

### 4.1 项目是什么？

项目用于保存一组相关的数据。一个项目下面可以有多个 Episode。

processor 负责接收采集端发送的数据，并把数据放入对应项目中。采集设备本身不在 processor 中完成采集。

### 4.2 查看已有项目

打开左侧 `Projects`，可以查看已有项目和接收到的数据。

[![项目列表功能](images/projects-list-annotated.png)](images/projects-list-annotated.png)

图中功能：

1. `搜索项目`：输入项目名称，快速找到项目；
2. `新建项目`：建立一个新的数据接收项目；
3. `项目信息`：查看项目名称、状态、工作流、实际输入、Episode 数量和接收统计；
4. `Edit Project`：修改项目名称、工作流和项目状态；
5. `Delete`：删除当前项目，删除后可在垃圾桶中查看；
6. `展开接收批次`：点击项目行或右侧箭头，查看已接收的批次和 Episode。

`active` 表示项目正在使用，可以继续接收数据；`paused` 表示项目暂停使用。

### 4.3 新建项目

点击右上角 `New Project`，填写项目基本信息。

[![新建项目功能](images/project-new-annotated.png)](images/project-new-annotated.png)

各项内容：

- `Project Name`：项目名称；
- `Workflow`：给项目绑定一个当前工作流，可以新建工作流，新建的工作流初始为空，也可以选择已有的工作流；
- `Status`：`active` 表示启用，`paused` 表示暂停；
- `Target Episodes`：计划接收的数据数量，`0` 表示不限制；
- `Description`：项目说明，可以不填写；
- `Save`：保存项目。

项目名称不能为空。保存新项目后，系统会询问是否进入工作流页面继续设置。

### 4.4 选择工作流

在新建项目页面的 `Workflow` 中选择对应工作流。

[![选择项目工作流](images/project-workflow-select-annotated.png)](images/project-workflow-select-annotated.png)

工作流列表功能：

1. 点击 `Workflow` 输入框打开工作流列表；
2. 点击 `+ New Workflow` 新建空白工作流；
3. 带 `Template` 标记的是模板工作流；
4. 点击已有工作流名称，将它绑定到当前项目；
5. 点击右侧按钮，进入工作流页面编辑该工作流。

一个项目使用一个当前工作流。重新选择其他工作流时会另存为新的绑定当前的工作流。

选择原则：

- RGB 数据选择 RGB 相关工作流；
- RGB 加 Depth 选择 RGB-D 相关工作流；
- 有手套传感器时选择包含 Glove Sensor 的工作流；
- 不确定时不要随便选择，先确认项目输入数据类型。

### 4.5 接收数据流程

```text
采集端生成数据
  ↓
采集端发送数据
  ↓
processor 接收数据
  ↓
解压和检查
  ↓
匹配项目和工作流
  ↓
项目中出现新的 Episode
```

### 4.6 接收数据时怎么操作？

1. 新建项目并选择正确的工作流；
2. 保持项目状态为 `active`；
3. 由采集端向 processor 发送数据；
4. 等待 processor 接收、解压和检查；
5. 展开项目，确认新的批次和 Episode 已经出现。

项目已经绑定工作流时，系统会处理新接收的数据。没有绑定工作流时，数据只会被接收和保存，不会执行后处理；之后绑定工作流并保存，系统会补充处理已有批次。

### 4.7 重要提醒

- 接收过程中不要关闭采集端的发送程序；
- 解压和检查完成前不要重复发送同一个数据；
- 每个 Episode 应该有对应的视频或传感器数据；
- 接收失败时先查看失败提示，再重新发送或重新处理；
- 项目和工作流不匹配时，不要强行运行。

## 5. 工作流

### 5.1 工作流是什么？

工作流决定系统如何处理接收到的数据。

```text
输入数据 → 处理数据 → 检查结果 → 导出数据
```

### 5.2 工作流页面

点击左侧 `Workflow` 进入工作流页面。

[![工作流页面](images/workflow-empty-annotated.png)](images/workflow-empty-annotated.png)

图中功能：

1. `工作流选择`：切换已有工作流或新建工作流；
2. `工作流名称`：查看或修改当前工作流名称；
3. `编辑画布`：放置节点并用连接线组成处理流程；
4. `节点列表`：提供输入、处理、审核和导出节点；
5. `Template`：从模板开始建立工作流；
6. `Save`：保存当前工作流。

### 5.3 从模板开始

点击右上角 `Template`，选择和接收数据匹配的模板。

[![选择工作流模板](images/workflow-template-annotated.png)](images/workflow-template-annotated.png)

1. `RGB-D_Workflow`：使用 RGB 和深度数据的处理链；
2. `Stereo-RGB_Workflow`：使用左、右两路 RGB 数据的处理链；
3. `Cancel`：不使用模板，关闭选择窗口。

新建或已有工作流都可以套用模板。选择模板会替换当前画布中的处理链，但不会改变当前工作流的名称。

模板只提供处理步骤。输入设备节点会根据项目实际接收的数据生成。画布中已经有未保存修改时，先保存再更换模板。

### 5.4 检查完整流程

下面是已经连接好的工作流示例。

[![完整工作流](images/workflow-editor-annotated.png)](images/workflow-editor-annotated.png)

1. `Stereo RGB Camera`：项目接收到的左、右 RGB 视频；
2. `Glove Sensor`：项目接收到的手套传感器数据；
3. `AI Annotation`：根据 RGB 视频生成任务文字和标注片段；
4. `Human Review`：把视频、传感器和标注结果交给人工审核；
5. `LeRobot Export`：按工作流设置准备导出数据；
6. `Save`：确认连接正确后保存工作流。

图中的视频、手套数据和 AI 标注会一起进入人工审核：

```text
Stereo RGB Camera ─┬→ AI Annotation ─┐
                   └─────────────┤
Glove Sensor ───────────────────┤
                                    ↓
                             Human Review
                                    ↓
                             LeRobot Export
```

### 5.5 AI 标注设置

打开 `AI Annotation` 节点的设置窗口，选择标注语言和 API。

[![AI 标注设置](images/workflow-ai-settings-annotated.png)](images/workflow-ai-settings-annotated.png)

1. `Label Language`：选择中文或英文标注；
2. `Saved API Profile`：选择已经保存的 API 方案；
3. `API Provider`：选择 API 厂商；
4. `API Model`：填写或选择使用的模型；
5. `API Key`：填写 API 密钥；
6. `API Base URL`：填写 API 地址，使用官方默认地址时可以留空；
7. `Test Connection`：保存前测试 API 是否可用；
8. `Save`：保存 AI 标注设置。

> API 密钥应显示为隐藏状态。不要分享显示完整密钥的截图。

### 5.6 工作流模块说明

右侧模块分为 `INPUT`、`PROCESS`、`REVIEW` 和 `EXPORT` 四组。把模块拖到画布中，再按照输入和输出连接。

#### INPUT：输入数据

| 模块                | 接收的数据             | 提供的数据                             | 什么时候使用                                      |
| ------------------- | ---------------------- | -------------------------------------- | ------------------------------------------------- |
| Glove Sensor        | 手套压力和关节数据     | Glove Sensor Data                      | 项目中有手套传感器时使用                          |
| RGB Camera          | 单路彩色视频           | RGB Video                              | 单目普通相机、单目鱼眼相机或 RGB-D 相机的彩色画面 |
| RGB-D Camera        | 彩色视频和真实深度流   | RGB Video、Depth                       | 单个 RGB-D 相机，有深度数据                       |
| Stereo RGB Camera   | 左、右两路彩色视频     | Left RGB Video、Right RGB Video        | 只有双目 RGB 视频时使用，不会直接提供真实深度     |
| Stereo RGB-D Camera | 左、右彩色视频和深度流 | Left RGB Video、Right RGB Video、Depth | 双目视频同时带有真实深度时使用                    |

#### PROCESS：处理数据

| 模块                 | 需要什么          | 产生什么   | 作用                                       |
| -------------------- | ----------------- | ---------- | ------------------------------------------ |
| Human Annotation     | RGB Video         | Annotation | 由人工建立和修改任务片段                   |
| AI Annotation        | RGB Video         | Annotation | 使用本地模型或 API 自动生成中文、英文标注  |
| RGB_TO_2D_BareHand   | RGB Video         | Hand 2D    | 识别裸手的视频中二维关键点                 |
| RGB_TO_2D_BlackGlove | RGB Video         | Hand 2D    | 识别黑手套的视频中二维关键点               |
| RGB-D_3D_BareHand    | RGB Video + Depth | Hand 3D    | 使用匹配的深度数据计算裸手真实三维关键点   |
| RGB-D_3D_BlackGlove  | RGB Video + Depth | Hand 3D    | 使用匹配的深度数据计算黑手套真实三维关键点 |

`RGB_TO_2D` 模块产生的是二维关键点。页面中的空间预览只是帮助查看，不作为真实米制 3D 数据导出。

#### REVIEW：检查数据

| 模块              | 作用                                                              | 结果                                               |
| ----------------- | ----------------------------------------------------------------- | -------------------------------------------------- |
| Human Review      | 由用户播放视频，检查关键点、深度、传感器和标注                    | 审核通过后输出 Reviewed Data                       |
| AI Quality Review | 自动检查视频能否解码、帧是否连续、是否黑屏或卡帧、AI 标注是否完整 | 检查通过后输出 Reviewed Data，异常数据进入人工处理 |

#### EXPORT：导出数据

| 模块           | 作用                   | 主要设置                                           |
| -------------- | ---------------------- | -------------------------------------------------- |
| LeRobot Export | 导出 LeRobot 数据集    | 选择 v2.1 或 v3.0；包含对应的 meta、data 和 videos |
| HDF5 Export    | 导出一个 HDF5 数据文件 | 使用 gzip 压缩保存                                 |

导出模块应连接在审核模块之后。这样只有检查通过的数据才会进入导出流程。

### 5.7 常见处理方式

普通 RGB：

```text
RGB → Hand 2D → Human Review → Export
```

RGB 和深度：

```text
RGB + Depth → Hand 3D → Human Review → Export
```

RGB 和手套传感器：

```text
RGB + Glove Sensor → Human Review → Export
```

### 5.8 重要规则

- 有 RGB 和 Depth，才能计算真实 3D；
- 只有 RGB 时，可以得到 2D 关键点；
- 只有 RGB 时显示的空间效果不等于真实 metric 3D；
- 输入不匹配时，对应步骤会被跳过。

看到 Input mismatch，说明工作流需要的数据和当前项目数据不匹配，需要换用正确的工作流。

## 6. 标注数据

### 6.1 选择需要标注的数据

点击左侧 `Annotation`。先在右侧展开项目，再选择一个 Episode。

[![标注数据列表](images/annotation-list-annotated.png)](images/annotation-list-annotated.png)

1. `Annotation`：进入标注页面；
2. 点击项目名称展开项目；
3. 点击 Episode 编号，加载视频和已有标注。

中间显示 `Select an episode to review` 时，表示还没有选择 Episode，不是系统故障。

### 6.2 查看和编辑标注

[![标注编辑页面](images/annotation-editor-annotated.png)](images/annotation-editor-annotated.png)

1. `Preview Options`：控制关键点、轨迹、深度和 3D 预览；
2. `RGB 和 2D 关键点`：查看原始画面和图片上的手部关键点；
3. `3D 手部空间`：查看空间中的手部关键点；
4. `深度伪彩色预览`：查看深度数据的网页显示效果；
5. `AI Annotate`：按工作流设置运行 AI 标注；
6. `标注片段`：点击右侧某个片段，查看或修改它；
7. `Set Start / Set End`：用当前帧设置片段的开始帧和结束帧；
8. `Save Changes`：保存片段名称和帧范围的修改。

### 6.3 人工修改标注

1. 在右侧点击需要修改的标注片段；
2. 检查开始帧、结束帧和任务名称；
3. 修改后点击 `Save Changes`；
4. 检查下方时间条中的标注块。

### 6.4 AI 标注

1. 确认工作流中已经设置 `AI Annotation`；
2. 按工作流设置选择中文或英文；
3. 点击 `AI Annotate`；
4. 等待标注片段生成；
5. 人工检查并修改不正确的内容。
6. 工作流中有这个功能会自动进行标注
7. 可能会因为网络波动导致一个小片段，没有对应的标注信息

## 7. 视频查看和审核

### 7.1 选择待审核数据

点击 `Video Review` 下的 `Reviewing`，在右侧选择一个待审核 Episode。

[![待审核数据列表](images/reviewing-list-annotated.png)](images/reviewing-list-annotated.png)

1. `Reviewing`：查看等待人工审核的数据；
2. `展开项目`：查看项目下的 Episode；
3. `选择 Episode`：打开数据，并查看帧率、帧数、相机和处理状态；
4. `Approve`：检查正确后，通过当前 Episode；
5. `Reprocess`：处理结果不正确时，重新运行项目绑定的工作流。

### 7.2 检查视频和处理结果

[![视频审核页面](images/reviewing-player-annotated.png)](images/reviewing-player-annotated.png)

1. `Preview Options`：显示或隐藏不同的预览内容；
2. `RGB 原图`：查看彩色视频；打开关键点显示后会叠加 2D 手部关键点；
3. `3D 手部空间`：检查空间中的手部关键点；
4. `深度伪彩色预览`：检查深度数据是否正常；
5. `只读标注片段`：检查任务名称和帧范围；
6. `播放和逐帧控制`：播放视频，或使用上一帧、下一帧检查细节；
7. `Approve`：确认当前 Episode 审核通过。

审核时先等待关联数据加载完成，再点击播放。RGB、2D、3D、深度和传感器画面应使用相同帧号显示。

### 7.3 深度画面说明

深度画面中的蓝色、绿色、黄色等颜色只是网页中的预览效果。

深度视频保存的是原始 12-bit 深度码，也不会把伪彩色图片写回原始数据。

### 7.4 常见显示问题

- 黑屏：等待当前 Episode 加载完成，再刷新页面；
- 没有 3D 手部：检查工作流是否包含 3D 处理，以及 Episode 是否有 Depth；
- 关键点不动：重新处理对应 Episode；
- 播放卡顿：等待关联数据完成缓存后再播放。

## 8. 已通过和导出

### 8.1 已通过数据

点击 `Video Review` 下的 `Approved`，可以查看已经通过审核的数据。

[![已通过和导出](images/approved-list-annotated.png)](images/approved-list-annotated.png)

1. `Approved`：进入已通过列表；
2. `多选模式`：点击页面顶部 `Select` 进入；进入后按钮变为 `Cancel`；
3. `Select All`：选择当前列表中的全部 Episode；
4. `Episode 复选框`：可以单选一条，也可以同时选择多条；
5. `Export`：导出这一条 Episode；
6. `Unreview`：取消审核通过状态，让数据回到待审核列表。

### 8.2 导出流程

导出一条 Episode：

1. 在该 Episode 的项目卡片中点击 `Export`；
2. 等待导出完成并下载压缩包。

批量导出：

1. 点击页面顶部 `Select`；
2. 勾选一条或多条 Episode，也可以点击 `Select All`；
3. 选择后页面会显示 `Batch Download` 操作条；
4. 点击 `Batch Download`，等待导出完成。

导出格式使用当前工作流导出节点中的设置，例如 LeRobot 2.1、LeRobot 3.0 或 HDF5，不需要进入独立导出页面。

### 8.3 导出内容

```text
项目名称/
├── meta/       # 说明和统计信息
├── data/       # 每帧数据、关键点和传感器数据
└── videos/     # 视频
```

后处理后的关键点、标注和手套传感器数据，会和对应的数据一起导出。

## 9. 失败处理

### 9.1 处理流程

```text
看到 Failed
  ↓
打开失败数据
  ↓
查看错误提示
  ↓
点击 Retry 或 Reprocess
  ↓
等待重新处理
```

> 图片位置：待补充“Failed 失败页面”截图。

### 9.2 常见错误

| 错误          | 处理方法                     |
| ------------- | ---------------------------- |
| 没有 RGB      | 检查是否上传了彩色视频       |
| 没有 Depth    | 不能计算真实 3D              |
| 工作流不匹配  | 选择正确的工作流             |
| AI 标注失败   | 检查 API 设置后重试          |
| 视频打不开    | 由采集端重新发送或联系管理员 |
| Worker 未运行 | 联系管理员重启服务           |

重试两次仍然失败时，请发送数据名称、错误截图、失败时间和操作步骤。

## 10. 垃圾桶

### 10.1 垃圾桶有什么作用？

删除的数据会先进入垃圾桶。

### 10.2 怎么操作？

[![垃圾桶页面](images/trash-annotated.png)](images/trash-annotated.png)

1. 点击左侧 `Trash` 进入垃圾桶；
2. 查看数据的剩余保留时间；
3. 需要保留时点击 `Restore` 恢复；
4. 确定不再需要时点击 `Delete` 永久删除；
5. `Purge All` 会永久删除垃圾桶中的全部数据。

### 10.3 重要提醒

- 垃圾桶中的数据可以恢复；
- 永久删除后通常不能恢复；
- 清空垃圾桶前要再次确认；
- 找不到数据时，先检查项目名称和删除时间。

## 11. 常见问题

### 页面打不开

检查服务是否启动。

### 数据一直处理中

刷新页面。如果长时间不变，可能是 Worker 没有运行。

### 深度画面是黑色

等待加载完成后刷新，并检查当前 Episode 是否真的有深度视频。

### 只有 2D，没有 3D

真实 3D 需要 RGB 和 Depth 两种输入。只有 RGB 时不能计算真实空间位置。

### 播放卡顿

等待数据加载完成后再播放。检查细节时可以使用上一帧和下一帧。

### AI 标注没有结果

检查 API 设置、模型、网络和语言设置，然后重新运行 AI 标注。

### 导出失败

确认数据已经审核完成，再重新导出。

## 12. 数据和程序说明

### 12.1 项目目录

```text
processor/
├── app/        # 后端、工作流、视频和导出
├── worker/     # 后台处理任务
├── web/        # 页面和前端代码
├── scripts/    # 启动、检查和维护脚本
├── tests/      # 自动测试
├── deploy.py   # 一键部署入口
└── .env        # 本机配置，不要提交到 Git
```

### 12.2 项目数据目录

```text
data/sessions/<project>/
├── data/
├── meta/
└── videos/
```

原始深度视频保存为深度码。浏览器显示的伪彩色不会写回原始数据。

### 12.3 启动检查

```bash
python deploy.py --check-only
python -m compileall app worker
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

不要把 .env、API Key、Token、真实数据和服务器凭据提交到 Git。

## 13. 代码结构与功能说明

本章用于说明页面背后的前端、后端、Worker 和数据处理代码。普通操作不需要修改这些文件；需要维护功能时，可以根据本章快速找到对应位置。

### 13.1 系统代码链路

    浏览器页面
        ↓
    HTML 模板和 JavaScript
        ↓
    FastAPI 后端接口
        ↓
    项目、Episode、标注和工作流
        ↓
    Worker 领取处理任务
        ↓
    工作流模块执行后处理
        ↓
    保存关键点、3D、传感器和标注结果
        ↓
    视频审核和数据导出

业务数据主要保存在项目文件中。用户账号可以使用数据库；数据库暂时不可用时，程序保留兼容回退能力。

### 13.2 代码目录树

    processor/
    ├── app/                         # Python 后端
    │   ├── main.py                  # FastAPI 启动入口
    │   ├── config.py                # 环境变量和系统配置
    │   ├── paths.py                 # 统一目录路径
    │   ├── localstore.py            # 项目、工作流和 Episode 文件状态
    │   ├── database.py              # 用户账号数据库连接
    │   ├── models.py                # 数据库模型
    │   ├── workflow_dispatch.py     # 工作流匹配、自动派发和历史回填
    │   ├── workflow_bindings.py     # 工作流与实际设备输入绑定
    │   ├── ai_annotation.py         # AI 标注服务
    │   ├── export_engine.py         # 导出任务公共逻辑
    │   ├── lerobot_export.py        # LeRobot 3.0 数据生成
    │   ├── lerobot_v21.py           # LeRobot 2.1 兼容处理
    │   ├── hdf5_export.py           # HDF5 数据生成
    │   ├── routes/                  # 页面和业务接口
    │   ├── api/                     # 项目、工作流和 Worker 接口
    │   └── processing/              # 工作流后处理框架
    │       ├── registry.py           # 自动注册处理模块
    │       ├── catalog.py            # 向工作流页面提供模块清单
    │       ├── batch.py              # 查找批次中的视频和 Parquet
    │       └── modules/              # 每个工作流节点的执行代码
    ├── worker/                      # 后台处理程序
    │   ├── __main__.py              # Worker 启动入口
    │   ├── runner.py                # 领取和执行工作流任务
    │   └── client.py                # 与后端 Worker API 通信
    ├── web/                         # 前端页面
    │   ├── templates/               # HTML 页面模板
    │   ├── static/js/               # 页面交互、播放和渲染
    │   └── workflow-studio/         # React 工作流编辑器源码
    ├── scripts/                     # 部署、迁移和维护脚本
    ├── tests/                       # 自动测试
    ├── models/                      # 本地模型文件
    ├── docs/                        # 中文和英文项目文档
    ├── deploy.py                    # 一键部署入口
    └── requirements*.txt            # Python 依赖

<code>.venv*</code>、<code>.pytest_cache</code> 和 <code>.backups</code> 是本机环境、测试缓存或备份，不属于主要业务代码。

### 13.3 前端代码

前端由两部分组成：

1. 普通页面使用 HTML 模板和原生 JavaScript；
2. 工作流编辑器使用 React 和 TypeScript。

#### 页面模板

| 文件 | 作用 |
| --- | --- |
| <code>web/templates/base.html</code> | 公共页面框架、左侧菜单和通用资源 |
| <code>web/templates/overview.html</code> | 首页 |
| <code>web/templates/tasks.html</code> | 项目管理页面 |
| <code>web/templates/index.html</code> | 标注和视频审核页面 |
| <code>web/templates/trash.html</code> | 垃圾桶页面 |
| <code>web/templates/login.html</code> | 登录页面 |
| <code>web/templates/users.html</code> | 用户管理页面，目前仍在开发 |
| <code>web/templates/workflow_studio.html</code> | 工作流编辑器入口 |

#### 页面 JavaScript

| 文件 | 作用 |
| --- | --- |
| <code>web/static/js/dashboard.js</code> | 首页统计、最近数据和刷新 |
| <code>web/static/js/tasks.js</code> | 项目创建、编辑、删除和批次展开 |
| <code>web/static/js/app.js</code> | Episode 列表、审核、删除和导出操作 |
| <code>web/static/js/player.js</code> | 视频加载、统一播放时钟和帧同步 |
| <code>web/static/js/annotations.js</code> | 标注片段、时间条和逐帧标注 |
| <code>web/static/js/slice-preview.js</code> | 右上角标注片段预览 |
| <code>web/static/js/hand-overlay.js</code> | 在 RGB 原图上绘制 2D 手部关键点 |
| <code>web/static/js/depth-renderer.js</code> | 将原始 12-bit 深度码渲染为网页伪彩色 |
| <code>web/static/js/heatmap.js</code> | 手套传感器数据同步显示 |
| <code>web/static/js/media-cache.js</code> | 使用 IndexedDB 缓存关键点和传感器数据 |
| <code>web/static/js/i18n.js</code> | 中英文界面文字 |
| <code>web/static/js/login.js</code> | 登录表单 |
| <code>web/static/js/users.js</code> | 用户管理交互，目前仍在开发 |

工作流编辑器源码位于 <code>web/workflow-studio/src/</code>。编译后的文件位于 <code>web/static/workflow-studio/</code>，修改工作流页面时应修改源码并重新构建，不要直接修改编译结果。

### 13.4 后端入口和页面接口

<code>app/main.py</code> 是后端入口，主要负责：

- 启动 FastAPI；
- 初始化文件目录和用户数据库；
- 预热项目与 Episode 元数据缓存；
- 启动上传处理队列；
- 注册页面、项目、视频、标注、工作流和导出接口；
- 提供静态文件缓存和大媒体文件传输规则。

| 文件 | 作用 |
| --- | --- |
| <code>app/routes/pages.py</code> | 返回首页、项目、审核、工作流和垃圾桶页面 |
| <code>app/routes/dashboard.py</code> | 首页统计、最近 Episode 和趋势 |
| <code>app/routes/session.py</code> | 接收压缩包、解压、整理目录和导入元数据 |
| <code>app/routes/ingestion.py</code> | Episode 列表、详情、审核、重跑、删除和恢复 |
| <code>app/routes/video.py</code> | RGB 视频、深度码、深度预览、2D、3D 和传感器接口 |
| <code>app/routes/annotations.py</code> | 创建、修改、删除和逐帧读取标注 |
| <code>app/routes/export.py</code> | 单个导出、批量导出、任务状态和下载 |
| <code>app/routes/auth.py</code> | 登录、退出和当前用户 |
| <code>app/routes/devices.py</code> | 采集设备心跳和输入能力 |

### 13.5 项目、工作流和 Worker API

| 文件 | 作用 |
| --- | --- |
| <code>app/api/projects.py</code> | 项目创建、编辑、删除、输入源和工作流绑定 |
| <code>app/api/workflows.py</code> | 工作流创建、保存、运行、使用情况和重试 |
| <code>app/api/worker.py</code> | Worker 领取任务、下载输入、心跳、完成和失败上报 |
| <code>app/api/exceptions.py</code> | 汇总和清理处理异常 |
| <code>app/api/users.py</code> | 用户创建、角色、状态和删除，目前仍在开发 |

后端与 Worker 的任务过程：

    后端创建运行记录
        ↓
    Worker 领取任务
        ↓
    Worker 下载 Episode 输入
        ↓
    执行工作流节点
        ↓
    Worker 上传结果并报告完成
        ↓
    Episode 进入审核或通过状态

Worker 会定期发送心跳。如果 Worker 中断，任务不会立刻丢失，后端可以在超时后重新安排。

### 13.6 工作流后处理模块

<code>app/processing/modules/</code> 中一个 Python 文件通常对应一个工作流节点。模块通过注册表自动出现在工作流编辑器中，并由 Worker 执行。

| 模块文件 | 作用 |
| --- | --- |
| <code>mono_camera.py</code> | 单路 RGB 输入 |
| <code>rgbd_camera.py</code> | RGB 和深度输入 |
| <code>stereo_camera.py</code> | 左、右双目 RGB 输入 |
| <code>stereo_rgbd_camera.py</code> | 左、右 RGB 和深度输入 |
| <code>glove_sensor.py</code> | 手套传感器输入 |
| <code>mediapipe_hand.py</code> | MediaPipe 手部关键点和手势识别 |
| <code>rgb_hand_3d.py</code> | 裸手 RGB 二维关键点和预览空间效果 |
| <code>black_hand_rgb_3d.py</code> | 黑手套 RGB 二维关键点和预览空间效果 |
| <code>depth_hand_3d.py</code> | RGB-D 手部模块使用的真实 3D 计算辅助代码，不是独立节点 |
| <code>black_glove_hand.py</code> | 黑手套关键点处理 |
| <code>ai_annotation.py</code> | 声明并触发 AI 自动标注 |
| <code>annotation.py</code> | 人工标注工作流节点 |
| <code>human_review.py</code> | 人工审核门 |
| <code>ai_quality_review.py</code> | 视频和标注自动质量检查 |
| <code>lerobot_export.py</code> | LeRobot 导出节点 |
| <code>hdf5_export.py</code> | HDF5 导出节点 |

增加工作流模块时，应同时检查：

1. 模块输入和输出类型；
2. Worker 是否能够执行；
3. 工作流编辑器是否正确显示；
4. 处理结果是否能在审核页面读取；
5. 导出模块是否包含新增结果。

### 13.7 数据、缓存和导出

| 文件 | 作用 |
| --- | --- |
| <code>app/localstore.py</code> | 扫描和缓存项目、Episode、工作流及运行状态 |
| <code>app/storage.py</code> | 文件上传、校验、保存和远程同步 |
| <code>app/remote_storage.py</code> | 远程存储访问 |
| <code>app/media_groups.py</code> | 组织 RGB、深度和传感器媒体组 |
| <code>app/media_cache.py</code> | 服务端媒体缓存 |
| <code>app/browser_preview.py</code> | 生成浏览器兼容的视频预览 |
| <code>app/artifact_resolver.py</code> | 查找工作流处理结果 |
| <code>app/export_engine.py</code> | 统一导出流程 |
| <code>app/lerobot_export.py</code> | 生成 LeRobot 3.0 数据集 |
| <code>app/lerobot_v21.py</code> | 生成和兼容 LeRobot 2.1 数据 |
| <code>app/hdf5_export.py</code> | 生成 HDF5 数据集 |

源数据、后处理结果和导出副本需要分清：

    data/sessions/    # 项目源数据和合并后的后处理字段
    data/tmp/         # 临时处理文件
    浏览器 IndexedDB # 前端预览缓存，可以重新生成
    导出压缩包        # 根据工作流设置生成的交付文件

不要把浏览器伪彩色深度图当作源数据保存。源深度仍然是原始深度码。

### 13.8 修改功能时去哪里

| 需要修改的功能 | 前端位置 | 后端或处理位置 |
| --- | --- | --- |
| 首页统计 | <code>dashboard.js</code> | <code>routes/dashboard.py</code> |
| 项目页面 | <code>tasks.js</code> | <code>api/projects.py</code> |
| 上传和解压 | 项目页面 | <code>routes/session.py</code> |
| 工作流编辑器 | <code>workflow-studio/src/</code> | <code>api/workflows.py</code> |
| 工作流自动运行 | 项目状态显示 | <code>workflow_dispatch.py</code> |
| RGB 视频播放 | <code>player.js</code> | <code>routes/video.py</code> |
| 播放帧同步 | <code>player.js</code> | <code>routes/video.py</code> |
| 2D 手部关键点 | <code>hand-overlay.js</code> | 手部处理模块 |
| 3D 手部空间 | <code>player.js</code> | <code>processing/modules/*3d*.py</code> |
| 深度伪彩色 | <code>depth-renderer.js</code> | <code>routes/video.py</code> |
| 手套传感器 | <code>heatmap.js</code> | <code>glove_sensor.py</code> |
| 标注时间条 | <code>annotations.js</code> | <code>routes/annotations.py</code> |
| AI 标注 | <code>annotations.js</code> | <code>ai_annotation.py</code> |
| 视频审核 | <code>app.js</code> | <code>routes/ingestion.py</code> |
| 单个和批量导出 | <code>app.js</code> | <code>routes/export.py</code> |
| LeRobot 格式 | 导出按钮 | <code>lerobot_export.py</code>、<code>lerobot_v21.py</code> |
| HDF5 格式 | 导出按钮 | <code>hdf5_export.py</code> |
| 垃圾桶 | <code>trash.html</code> | <code>routes/ingestion.py</code> |
| 用户管理 | <code>users.js</code> | <code>api/users.py</code> |

### 13.9 排查问题的顺序

遇到问题时，建议按照下面顺序检查：

1. 先确认问题出现在哪个页面；
2. 找到该页面对应的 JavaScript；
3. 在浏览器开发者工具中检查接口是否失败；
4. 根据接口地址找到对应的 Python 路由；
5. 如果是工作流任务，再检查 Worker 和处理模块；
6. 如果是显示不同步，再检查当前 Episode 的帧数、帧率和缓存；
7. 修改后运行测试，再打开真实数据验证。

## 结尾

先上传，再处理；处理完成后检查；检查正确后再导出。

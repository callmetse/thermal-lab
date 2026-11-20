# MET / CLO：正式期刊证据与复用路线

核查日期：2026-11-02，Asia/Tokyo。本轮主证据只纳入已正式发表的期刊论文，不用预印本支持 MET/CLO 有效性结论。优先关注建筑环境与热舒适领域的 Building and Environment、Energy and Buildings；未查询指定年度的 JCR/中科院分区，不把“顶刊”当作已核验的分区标签。

八篇核心文献的 DOI、题名、期刊、发表日期和 `journal-article` 类型已通过 Crossref API 独立核对，见 [crossref_verified.json](crossref_verified.json)。联网时部分 ScienceDirect 全文请求返回 403；随后在本地论文库找到 P4、P6 原版 PDF，核对了关键正文页，详见 [全文设置与复用模块](P4_P6_全文设置与可复用模块.md)。其他论文目前主要依据出版社摘要及作者机构记录，未核实的全文参数不补写。

## 1. 对本项目最有用的正式论文

| 编号 | 论文与发表信息 | 已确认的方法／设置 | 对我们有什么用 | 不能直接照搬的部分 |
|---|---|---|---|---|
| P1 | Choi 等，2022，*Deep-vision-based metabolic rate and clothing insulation estimation for occupant-centric control*；Building and Environment 221, 109345；[DOI](https://doi.org/10.1016/j.buildenv.2022.109345)；[作者机构记录](https://yonsei.elsevierpure.com/en/publications/deep-vision-based-metabolic-rate-and-clothing-insulation-estimati/) | 两类活动、两套服装；五名参与者的活动／服装识别约 97%；假设场景中的 MET/CLO 分类为 100%；另有八人热舒适控制实验 | 第一轮先做少量、定义清楚的衣着和活动，不一开始追求任意衣物、任意活动；同时测识别与最终估计 | 100% 属于很小的受控任务，不能解释成任意环境中物理代谢率／热阻测量正确率 |
| P2 | Yun 等，2024，*Performance evaluation of an occupant metabolic rate estimation algorithm using activity classification and object detection models*；Building and Environment 252, 111299；[DOI](https://doi.org/10.1016/j.buildenv.2024.111299)；[机构摘要](https://cau.scholarworks.kr/item/d8def121-910a-4766-b90e-692482c55eb6) | 姿态活动分类＋六种室内交互物体；物体即时分类 89%，15 秒众数 100%；六种 MET 即时估计 83%，15 秒众数 99% | **直接加入“仅姿态 vs 姿态＋物体上下文”“即时 vs 时间众数”消融。** 同时测活动切换后的更新延迟 | 15 秒众数改善稳态分类，同时会增加状态变化延迟；不是新的 15 FPS 指标。物体与任务类别、MET 标定细节还需全文确认 |
| P3 | Wei 等，2024，*Real-time clothing insulation level classification based on model transfer learning and computer vision for PMV-based heating system optimization through piecewise linearization*；Building and Environment 253, 111277；[DOI](https://doi.org/10.1016/j.buildenv.2024.111277)；[Cardiff 作者记录](https://orca.cardiff.ac.uk/id/eprint/178033/) | 两阶段人物检测＋服装轻／中／重三级分类；出版社 highlights 指明 YOLO＋GoogLeNet；大学建筑初步现场验证，节能另含情景仿真 | 若精细衣物类别可靠性不足，可以额外比较粗粒度衣着等级；**人体 ROI 分类是成熟路线** | 三级标签不是连续 CLO 真值；ROI、训练资料、三级边界、实际相机参数不能凭摘要编造；该机构库无全文 |
| P4 | Jung 等，2024，*Vision-based multi-label detection framework for capturing occupant action and clothing information using large-scale dataset*；Building and Environment 257, 111537；[DOI](https://doi.org/10.1016/j.buildenv.2024.111537)；[机构摘要](https://yonsei.elsevierpure.com/en/publications/vision-based-multi-label-detection-framework-for-capturing-occupa/) | 单阶段同时输出人体框、活动、服饰标签；研究报告 mAP 45.0%，相对多阶段框架推理速度约加倍 | 优先考虑现成联合模型，避免永久堆三个检测器；作为后续多阶段／单阶段对照 | mAP 属于论文任务，不能与当前 Fashionpedia 子集数字直接比较。论文给出的 [代码地址](https://github.com/shoonjung/Occupant_Act_Clo_Detection) 本轮在 GitHub 工具和公开网页均返回 404；未拿到可验证权重，**当前不能列为即装即用** |
| P5 | Jung 等，2025 年 1 月，*Occupant activities and clothes detection based on semi-supervised learning for occupant-centric thermal control*；Building and Environment 267, 112178；DOI 年份为 2024；[DOI](https://doi.org/10.1016/j.buildenv.2024.112178)；[作者记录](https://yonsei.elsevierpure.com/en/publications/occupant-activities-and-clothes-detection-based-on-semi-supervise/) | 多任务模型＋半监督学习＋双阶段训练方法，论文含消融 | 将来确有数据不足时借用训练策略，不自行设计新网络 | 本轮用户要求不训练／不微调，因此先列候选，不运行训练来制造不可解释的主对照差异 |
| P6 | Sun 等，2025，*Dynamic detection models for clothing insulation and metabolic rate based on computer vision*；Building and Environment 283, 113372；[DOI](https://doi.org/10.1016/j.buildenv.2025.113372)；本地全文已核对 | YOLOv8n 衣物＋YOLOv7-pose/TSSTG；热人模与 K5 间接量热赋值；服饰 0.5 秒检测、10 秒众数；动作每秒输出；动态动作平均 97.5%、最高 98.9% | 直接借多速率处理、带时间证据的完整服装缓存和动态切换评估；详见全文设置文档 | MET 数值是四类活动的实测均值赋值，不是逐人连续量热；季节与隐蔽衣层属于先验。骨架采样合同、可直接部署的权重仍需核查，不能把摘要最高值当平均值 |
| P7 | Meng 等，2025，*A visual prediction method for individual metabolic rate by integrating movement speed and physiological characteristics*；Energy and Buildings 340, 115779；[DOI](https://doi.org/10.1016/j.enbuild.2025.115779)；[出版社](https://www.sciencedirect.com/science/article/abs/pii/S0378778825005092) | OpenPose 骨架、身体运动速度与生理特征；20 名参与者实验 | MET 的后续路线应考虑运动强度和已确认的任务信息，而不只是“坐／站／走”类别 | 不能直接把像素／秒当米／秒，需相机几何标定；本项目不从外表推断性别／身份，体重等确有需要时应由本人提供；不把论文中的机械动能构造未经验证地当代谢能 |
| P8 | Li & Feng，2026，*Benchmarking zero-shot vision-language models against supervised learning for concurrent metabolic rate and clothing insulation estimation*；Building and Environment 294, 114408；[DOI](https://doi.org/10.1016/j.buildenv.2026.114408)；[作者实验室发表列表](https://sbelab.forestry.ubc.ca/publication/) | UE5 生成 2250 段合成视频，专家按 ASHRAE 55 标注；比较多任务 CNN 与 Gemini 3 Pro／2.5 Flash；Gemini 3 Pro 在该基准上 MAE 0.31 MET／0.021 CLO | **给“无需本任务训练的 VLM 数值估计”提供了正式期刊对照设计。** 可以独立增加 7B VLM 分支，固定图片／视频证据、提示词与时间预算 | 合成、专家标准赋值不等于现场实测真值；Gemini 结果不能迁移成我们 Qwen 7B 的指标；论文中每段 clip 的延迟不能当每帧延迟。数据和权重下载可用性本轮未确认 |

## 2. 已发表，但硬件不匹配的路线

[Liu 等，Sensors 2022, 22(2), 619](https://doi.org/10.3390/s22020619)，*Clothing Insulation Rate and Metabolic Rate Estimation for Individual Thermal Comfort Assessment in Real Life*，采用热像、YOLOv5＋DeepSORT、皮肤／衣物表面温度、光流及随机森林。它是正式期刊论文，**不作为本轮“顶刊主证据”**。

可以借用其按人跟踪、分别提取服饰和运动证据的方法；其通过温差推导 CLO 的部分需要热像与环境条件，普通 Pi RGB 相机没有这些输入。不要删掉热像输入后照搬方程，还宣称复现了其数值精度。

## 3. 直接改变我们的实验设置

1. **感知与物理估计分开。** 分别记录人物、服饰、姿态、动作的标签结果，以及 CLO/MET 的数值、赋值依据、是否经过人工确认。不是先把衣物和动作改名成 CLO/MET。
2. **活动＋物体上下文。** 借 P2 比较仅骨架、骨架＋可交互物体；不能仅因手边有电脑就肯定在打字。先检查 COCO 预训练检测器实际有哪些所需类，再确定活动范围。
3. **时间聚合。** 首先加入 P2 的“即时 vs 15 秒众数”两个设置；1／3／5 秒为我们拟定的中间消融，须标明并非论文复现参数。评价稳态 F1、活动变化延迟、误翻转率，不能只看平滑后的高准确率。
4. **服饰由粗到细。** 精细类别和轻／中／重是两个任务；标签映射须固定并说明依据，不能将二者准确率混排。
5. **多人／遮挡／换衣。** 借 P1、P6 的小规模受控设计先做少数明确场景，再加多人交叉与遮挡。按人／片段留出，避免相邻帧同时出现在调参集和最终测试集。
6. **VLM 独立消融。** A/B/C 主对照使用同一文本 7B 和同一结构化证据；另外的“7B VLM 看图／片段”实验必须给各组相同额外视觉证据，并单列上传流量和推理时间。
7. **频率逐级测量。** A/B/C 的 8／12／15 FPS 扫描是工程试验设计，不是上述论文已验证的最优频率。论文摘要未披露的 FPS、相机距离、阈值一律待核查。

## 4. 不训练时，实际能复用到什么程度

| 内容 | 当前状态 | 本轮用法 |
|---|---|---|
| Ultralytics 人体检测、姿态、ByteTrack、NCNN 导出 | 官方实现可查，现有小模型已独立复制 | 主对照的工程基线，无新增训练 |
| 已有 clothing_best 与 TCN | 是旧实验已训练好的工件，有独立文件和哈希；并非本轮新训练，也并非这些期刊的公开模型 | 用于先打通三组相同视觉任务，**只称现有工程基线**，不宣传为新论文模型复现 |
| PaddleClas PULC / PP-Human | 官方项目有实现和公开属性模型 | 候选服饰属性对照；需要有效人体裁剪及可见性门控，输出只保留本任务所需衣物属性 |
| MMAction2 ST-GCN / STGCN++ / PoseC3D | 官方模型库有配置和 checkpoint | 候选时序模型；先核对 2D／3D 骨架布局、采样长度、目标类别、ARM 推理后端，不能只换文件名 |
| P4 联合模型 | 论文确认已发表，但原代码 URL 本轮 404，权重未取得 | 待找到作者更名仓库／公开工件后优先运行官方样例；当前不临时重写其网络 |
| P6 动态数值模型 | 摘要方法与结果已确认；可直接部署的权重未确认 | 先借标签与动态评估设计；不把自己的规则命名成 P6 复现 |
| P8 零样本 VLM 评估 | 正式发表；合成数据与模型设置已确认，下载入口待核查 | 仅作为后续独立数值估计支线，不能把合成基准当现场验收 |

没有找到经过验证、可即装即用且适配我们 RGB/Pi 场景的期刊模型，**不等于模型不存在**。本轮证据不足时保留工件状态，而不是自己搭一个近似方案冒充成熟复现。

## 5. CLO/MET 的准确性怎么报告

- 衣物／活动真值：人工按视频片段标注；低采样、遮挡、未知也要记录。所有应答帧算端到端覆盖率，另外报告“作出预测时的准确率”，避免只保留高置信输出。
- 标准参考真值：明确衣着清单、层次、任务、强度后，使用统一版本标准／[pythermalcomfort](https://pythermalcomfort.readthedocs.io/en/latest/documentation/clothing.html) 的表或实现。误差命名为“相对标准参考值的误差”，不要称生理／物理实测误差。
- 实测真值：需要可靠的衣物热阻测量或间接量热等独立依据。没有这些数据时，物理 CLO/MET MAE 写“未测”，不拿查表输出再当标签算出零误差。
- 边界：躺着不等于睡觉；坐着也可能进行高负荷活动。隐藏内层可作为模型假设研究，不能被 LLM 自动补成已知事实。

## 6. 仍需精读全文的清单

优先顺序：P2 → P6 → P4 → P8 → P3。逐篇提取：相机类型／视角／距离、被试与衣着／活动范围、真实采样率、模型输入窗口、稳定输出频率、物理测量仪器、赋值表版本、训练／验证／测试划分、是否按人留出、CPU/GPU 与后端、总链路延迟、代码／数据／权重许可。当前未拿到的项明确写“未核实”。

不因获取全文受限而编造实验参数，也不把摘要中高准确率直接当我们系统的预期表现。

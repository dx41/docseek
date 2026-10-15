## 安装

### 方式一：deb 包（推荐）

```bash
sudo dpkg -i docseek_0.1.0_all.deb     # 装到 /opt/docseek，命令 docseek，菜单里有 DocSeek
```

包里的依赖已声明：`python3-gi`、`gir1.2-gtk-3.0`、`poppler-utils` 必装；
`tesseract-ocr`（含中文包）、`python3-pil`、`catdoc`、`libwps-tools`、LibreOffice 是推荐项
（缺了对应格式就抽不出正文，但程序照常运行）。缺依赖时：`sudo apt-get -f install`。

### 方式二：源码安装（装到家目录，不需要 root）

```bash
git clone https://github.com/dx41/docseek.git && cd docseek
./install.sh          # 装到 ~/.local/bin，生成桌面菜单项
```

**包内不含任何索引数据**：索引与缓存在首次运行时于
`~/.local/share/docseek/`（索引库）和 `~/.cache/docseek/`（OCR/渲染缓存）下自行生成，
卸载（`./uninstall.sh` 或 `sudo dpkg -r docseek`）不会带走这些数据。

## 快速开始

```bash
cd ~/docseek

# 1) 图形界面（首次进去在「索引设置」里选目录，然后点左上角 ⟳ 建索引）
./bin/docseek gui

# 2) 命令行
./bin/docseek index ~/文档 ~/下载      # 建立/更新索引（可随时 Ctrl-C，下次接着跑）
./bin/docseek search "人民代表大会"     # 搜正文
./bin/docseek search '预算 ext:xlsx path:2026 -草稿' --limit 20
./bin/docseek stats                    # 索引概况
./bin/docseek check ~/某个目录 --preview 80   # 抽取自检：看某格式能否抽到文本（不写库）
./bin/docseek ocr 扫描件.pdf            # 对单个文件跑 OCR，验证扫描件能不能搜
./bin/docseek cache                    # 查看缓存占用（--clear-ocr / --clear-render）
```

`bin/docseek` 会把项目根目录加进 `PYTHONPATH` 后调用 `python3 -m docseek.cli`，
所以也可以直接 `python3 -m docseek.cli ...`（在项目根目录下）。

安装桌面图标：

```bash
cp ~/docseek/docseek.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

## 检索语法

**文件名与路径也会被搜**：索引时把文件名、完整路径一并切词入库，所以直接拿文件名的词
（甚至目录名）都能搜到，不必记得正文里怎么写的。匹配判定用"文件名+路径+正文"，
但**命中片段只取自正文**（保证预览里的高亮定位准确）。

| 写法 | 含义 |
|---|---|
| `人民代表大会` | 内容检索（多词之间是 AND） |
| `"带 空格 的短语"` | 短语（内部空白宽松匹配） |
| `-草稿` | 排除含该词的文档 |
| `ext:pdf,docx` | 只搜指定类型（也接受中文逗号、顿号） |
| `path:2026` | 路径里含该片段 |
| `re:预算\d+万` | 正则（较慢，会退化为扫描） |
| `预算` | 单汉字会自动退化为全量扫描并提示 |

## 界面布局与快捷键

结果列表参照 TextSeek 的三行式行样式，每条结果占三行（视觉上就是"名称 / 大小 / 时间"三列）：

```
文件名（加粗，过长中间省略）                        23 KB   2015-05-13
    完整路径（小字、灰色，过长开头省略）
    …命中片段…（关键词红色加粗，最多 3 处，超出折叠为"另有 N 处"）
```

- **结果区右上角有排序下拉**：相关度（默认）/ 修改时间（新→旧、旧→新）/ 大小（大→小、小→大）/
  目录 / 类型。选择会记住（存在配置里），命令行为 `docseek search "词" --sort size_desc`。
  排序在**截断之前**生效，所以"取前 N 条"拿到的确实是排序后的前 N 条；
  它是在**本次命中的结果集内**排序（不是把整个索引重排）。
- 右侧是预览区，三个页签：**全文**（关键词高亮、自动滚到第一处命中）/ **版面**（原页渲染成图）/
  **命中**（该文档所有命中清单）。
- 想要 TextSeek 那种**通栏列表**：`Ctrl+P` 收起预览（再按恢复），或点右上角菜单里的
  "显示/隐藏预览"。

| 快捷键 | 作用 |
|---|---|
| `Enter` | 搜索 |
| `↑` / `↓`（搜索框内） | 翻搜索历史（像终端那样） |
| `Ctrl+H` | 展开搜索框右侧的历史下拉 |
| `Ctrl+P` | 显示/隐藏预览（通栏列表） |
| `F5` | 更新索引（pending 会自动补；**失败的不重试**） |
| 菜单→重试之前失败的文件 | 重试 `error` 的文件（等价 `--retry-errors`） |
| `Ctrl+F` | 聚焦搜索框 |
| `Esc` | 清空搜索框 |
| `双击结果` | 用系统默认程序打开文件 |
| `右键结果` | 打开文件 / 打开所在目录 / 复制路径 / 重新加载预览 |

**搜索历史**：自动记录每次搜索（最多 50 条，存 `~/.config/docseek/history.json`）。
点搜索框右边的时钟按钮（或 `Ctrl+H`）打开"最近搜索"面板：点条目直接重搜，
每条右侧的 ✕ 删除单条，面板右上角的垃圾桶按钮一键清空。

## 依赖

本机（麒麟 V10 SP1 / aarch64）实测全部已具备：

| 用途 | 组件 | 状态 |
|---|---|---|
| 界面 | GTK 3.24 + PyGObject 3.36 | 已装 |
| 正文高亮/代码着色 | GtkSourceView 3.24（可选） | 已装 |
| PDF | poppler-utils（pdftotext / pdftoppm / pdfinfo） | 已装 |
| docx/xlsx/pptx/odt/epub | 仅用 Python 标准库 zipfile + ElementTree | 无需安装 |
| 旧格式 .doc/.wps/.et | LibreOffice 6.4（headless，可关） | 已装 |
| .xls | python3-xlrd | 已装 |
| 图片/扫描件 OCR | tesseract 4.1 + 语言包 chi_sim / chi_tra / eng | 已装 |

可选增强（`apt` 一条命令，装上会自动被优先使用）：`catdoc`、`antiword`、`libwps-tools`
（`.wps` 直接解析，比走 LibreOffice 快）、`python3-jieba`。

## OCR（图片型 PDF / 扫描件）

**默认开启**：`pdftotext` 抽不出文字的页面会自动走 OCR，所以纯扫描的 PDF 也能按内容搜到；
"部分页面是扫描件"的混合文档同样覆盖（有文本层的页不重复识别）。

工作原理与代价（每一步都有实测依据，详见 `docseek/extract/ocr.py` 顶部注释）：

- 只对**没有文本层**的页做识别（一页文本少于 `ocr_min_page_chars`＝12 字即视为扫描页）；
- **优先直接抽取页内嵌的图片**（`pdfimages -j`，实测 0.01–0.2 秒/页）——扫描页往往就是
  一张内嵌图，用 `pdftoppm` 重绘成位图要 20–30 秒/页，纯属浪费；抽不到才回退渲染，
  且渲染前按 `ocr_max_pixels` 折算 `-scale-to`，不画巨图；
- **送进识别前等比缩小到 `ocr_max_pixels`＝4M 像素**（耗时几乎与像素数成正比）；
- **tesseract 强制单线程**（`OMP_THREAD_LIMIT=1`）：本机 4 线程反而慢 8.5 倍，
  吞吐靠 `ocr_workers`＝3 个进程并行拿；
- 识别结果过短时用 OSD 判方向、**转正后重试一次**（横躺的扫描件不转会全是乱码）；
- 综合后**每页约 5–17 秒**（原先 30–140 秒），3 路并行下整体吞吐再 ×3；
- 单份 PDF 最多识别 `ocr_max_pages`＝40 页，超出的页跳过并在结果里提示；
- 识别结果按 `(路径, mtime, 大小, 页码, 语言, dpi)` 缓存到 `~/.cache/docseek/ocrcache`，
  所以**首次索引慢、之后检索和预览都是毫秒级**（实测首抽 14 s → 命中缓存 0.2 s）；
- 图片文件（jpg/png…）默认**不**纳入索引（照片目录逐张识别代价太大），设置里可打开 `ocr_images`。

设置位置：界面「索引设置」里的 OCR 段；命令行对应 `~/.config/docseek/config.json` 的
`ocr_enabled / ocr_langs / ocr_dpi / ocr_max_pages / ocr_min_page_chars / ocr_images`。
单文件验证：`docseek ocr 某个扫描件.pdf`。

## 数据位置

| 内容 | 路径 |
|---|---|
| 配置 | `~/.config/docseek/config.json` |
| 搜索历史 | `~/.config/docseek/history.json`（最多 50 条，可在界面里逐条删除或清空） |
| 索引库 | `~/.local/share/docseek/index.db`（SQLite + FTS5；**正文也压缩存库**，检索不必现抽） |
| 渲染 / OCR 缓存 | `~/.cache/docseek/`（PDF 转换、页图、`ocrcache/`、LibreOffice 临时目录） |

**程序只会清理 `~/.cache/docseek/` 下自己创建的临时目录，绝不改动、移动或删除你的文档。**
菜单里的「清理版面渲染缓存」与 `docseek cache --clear-ocr/--clear-render` 也只作用于这些缓存
（逐条做白名单校验）。

## 实测性能（本机 aarch64）

| 操作 | 耗时 |
|---|---|
| docx → 文本（zip + XML） | 7 ms |
| pdf → 文本（pdftotext） | 20 ms |
| xls → 文本（xlrd） | 20 ms |
| .et → 文本（LibreOffice + CSV） | 0.9 s |
| .doc → 文本（LibreOffice） | 1.1–1.7 s |
| 一页 150dpi 版面图（pdftoppm，冷） | 0.2–0.9 s |
| 一页版面图（命中缓存） | <1 ms |
| OCR 一页扫描件（300dpi，chi_sim+eng，首次） | 10–20 s |
| OCR 一页（命中 OCR 缓存） | ~0.2 s |
| 搜索（56k 级索引，命中若干候选） | 首次毫秒~数十毫秒（正文存库，不再现抽） |

全机规模参考：`/home` 33,415 份 + `/data` 22,974 份文档中，纯文本 + 新式 Office + PDF
（约 3.8 万份）首轮约 7 分钟；约 1.8 万份旧格式走 LibreOffice 兜底约 1.5–2 小时，
可后台跑一次，之后靠 size+mtime 指纹增量更新。

## 设计要点

```
扫描(inode+mtime+size 指纹) → 抽取(分档) → 索引(SQLite FTS5) → 检索(粗筛+原文校验) → 预览(按需渲染)
```

1. **抽取分档**：纯文本/源码直接读；docx/xlsx/pptx/odt/epub 用 `zipfile` + `ElementTree`
   （不用装任何库）；PDF 走 `pdftotext -layout`；`.xls` 走 `xlrd`；其余旧格式交给
   LibreOffice headless（表格类必须用 CSV 过滤器——`txt` 过滤器是 Writer 专用的，对
   表格必然失败）。无法解析的格式逐文件隔离成错误记录，不中断批处理。
2. **中文分词**：本机 SQLite 3.31 的 FTS5 **没有** trigram/ICU，所以入库前把中文切成
   2-gram（`中华人民共和国` → `中华 华人 人民 民共 共和 和国`），西文数字小写按词。
3. **入库前抹平汉字之间的空白**：`pdftotext -layout` 会在汉字间插空格，PDF 里短语还常被
   行宽截断。若不处理，bigram 会被切断成单字，查询永远进不了候选集（详见 `tokenizer.strip_cjk_gaps`）。
4. **索引用 `detail=none`**：不存位置与词频，因此写入前对 token 去重，索引体积最小。
5. **粗筛 + 原文校验**：FTS 只负责选出候选文档，随后重新抽取原文做精确校验并生成命中片段
   （带绝对偏移与页码）。所以"索引里有这个 token、正文里其实没有"的假阳性会被丢掉。
6. **宽松匹配**：精确匹配之外，还允许命中"字间被插空白"的写法；两者合并计数、重叠不重复。
7. **OCR 只补空页**：有文本层的页一律不动，只有"没有文本层"的页才渲染识别，并逐页缓存。

## 哪些文件"没进索引"，为什么

```bash
docseek problems                 # 列出所有非 ok 的文件：按状态、原因、扩展名分组 + 示例
docseek problems --ext pdf       # 只看 PDF
docseek problems --state error   # 只看真失败的
```

状态语义（`docseek stats` 里的状态分布就是这几个）：

| 状态 | 含义 | 会不会重试 |
|---|---|---|
| `ok` | 正常，有正文 | 指纹没变就跳过 |
| `empty` | 抽取成功但**确实没有文字**（照片、空白页、非文本文件） | 不重试（不是错误） |
| `pending` | 关着 OCR 时抽不到正文的扫描件 | **一定重试**，下次开启 OCR 自动补 |
| `error` | 真的没抽出来（文件损坏、旧格式缺解析器…） | 默认不重试，`--retry-errors` 可重试 |

常见原因与对策：

- **大量 jpg/png 显示"没有文字"**：照片本来就没文字。可选：设置里关掉"把图片文件也纳入索引"。
- **`~$xxx.docx` 之类**：Office 异常退出留下的临时锁文件，现在扫描阶段直接跳过。
- **旧格式 .doc/.wps/.xls 失败**：`sudo apt install catdoc libwps-tools`
  （比走 LibreOffice 又快又稳；麒麟源里**没有 antiword**，.doc 由 catdoc 覆盖）。
  装完在界面菜单点「**重试之前失败的文件**」，或命令行 `docseek index --retry-errors`。
- **`文件不是有效的 PDF`**：文件本身损坏或是几十字节的占位文件。

## 两轮索引策略（文档很多、又有扫描件时推荐）

```bash
docseek index "<目录>" --no-ocr    # 第一轮：只走有文本层的，快（几百份约一分钟）
docseek index "<目录>"             # 第二轮：自动只补第一轮没抽出正文的（pending）
```

第一轮里抽不到正文的文档会记成 **pending**（而不是"已完成"），所以第二轮一定会重跑它们；
反之已完成的部分靠 size+mtime 指纹直接跳过，不会重复干活。

## 索引相关说明

- **只有支持的格式才进索引**：图片/压缩包/音视频等按规则跳过；`--include-hidden` 控制隐藏文件；
  某些目录名（`node_modules`、`.git`、`__pycache__` 等）默认整棵跳过；软链目录默认不跟随
  （设置里可开"跟随符号链接目录"）。
- **索引随时可中断**：每 10 份或每 2 秒提交一次，中断不丢已完成的部分；再点一次 ⟳ 会接着跑。
- 收尾时界面会明确告诉你 `已处理 n/总数`、`还剩 N 份`、以及**跳过原因分类计数**
  （格式不支持 / 隐藏 / 排除目录 / 软链 / 超过体积上限），命令行同样输出。
- **关窗口会真正停下**：会终止索引线程并回收所有外部子进程（tesseract / pdftoppm / soffice），
  并带 4 秒硬兜底强制退出——不会出现"窗口关了 CPU 还在满"的情况。

## 已知限制

- **扫描件首次索引慢**：每页 OCR 10–20 秒（单份 PDF 上限 40 页）。识别结果会缓存，
  之后检索/预览都不再重认。不想要就关掉 OCR，或把 `ocr_max_pages` 调小。
- **OCR 有识别误差**：印章、手写、低分辨率扫描件的识别质量取决于原件，可能出现错字。
- **WPS 演示（.dps/.ppt/.pps）打不开**：LibreOffice 6.4 会把它们当纯文本导入并吐出乱码，
  程序用"乱码守卫"把这类结果判为失败而不是写进索引（宁缺勿滥）。
- **单汉字查询**会退化为全量扫描（bigram 索引无法覆盖单字），已给出提示。
- 索引库体积约为正文的 1–2 倍（中文 2-gram 的代价）。
- 索引只覆盖你在设置里选定的目录。

## 测试

```bash
cd ~/docseek && xvfb-run -a python3 -m unittest discover -s tests -v
```

200 个用例：分词 / 查询解析 / 宽松匹配 / 索引增量 / 端到端检索 / PDF 分页 / 旧格式 /
版面渲染 / OCR（语言解析、按页补抽、缓存、开关）/ 界面高亮偏移回归。
测试产物全部在 `tempfile` 临时目录里，不触碰用户文件；OCR 用例自带合成样本。

## 许可

本项目代码以 GPL-3.0 发布（如需其他许可请自行调整 `docseek/ui/window.py` 的关于对话框文案）。

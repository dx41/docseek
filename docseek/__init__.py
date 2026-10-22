"""DocSeek —— 本地文档内容搜索（银河麒麟 / GTK3）。

模块划分：
    config      配置与路径
    tokenizer   中文 bigram / 西文分词
    extract     文本抽取（plain / ooxml / pdf / legacy）
    index       SQLite FTS5 索引
    search      查询解析、校验、命中片段
    preview     版面渲染（PDF 页图、Office 转 PDF）
    ui          GTK3 界面
"""

__version__ = "0.1.6"
APP_ID = "net.local.docseek"
APP_NAME = "DocSeek"

# SummarySpec - 技术卡片结构化总结规范
"""
定义技术卡片的结构化总结规范（类似 skill 约束）：
- 受控分类词表（LLM 只能从指定分类里选，保证归档一致）
- 输出 JSON schema（结构化字段，供知识库存储与前端展示）
- prompt 模板（指导 LLM 生成高质量技术卡片）

设计目标：
1. 技术用户第一眼就能知道项目是什么、解决什么问题、用什么技术栈
2. 分类标签受控，便于归档到知识库、后续按分类检索
"""

import re

# 汉字区间：中文化判据的唯一依据
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 一张卡片的中文简介至少要含这么多汉字，才算「中文化」成功。
# 刻意不用「汉字占比」当判据：技术简介天然夹带大量英文术语
# （如 "WP2Shell PoC ... WordPress ... CVE-2026-6013"），占比会被拉到 0.2 以下，
# 按占比卡会把合格的中文简介误杀；而英文原文的汉字数为 0，两者一刀两断。
MIN_SUMMARY_CJK_CHARS = 10


def is_chinese_text(text, min_cjk: int = MIN_SUMMARY_CJK_CHARS) -> bool:
    """判断一段文案是否「以中文书写」。

    用于卡片简介的中文化闸门：LLM 偶尔会照抄英文原文（输入本身是英文描述时尤甚），
    一旦放过，卡片介绍就直接变成英文，而且因为已经写进知识库，只有下一轮采集
    再次碰到同一个仓库时才有机会被覆盖 —— 绝大多数情况下就是永久英文。
    """
    text = (text or "").strip()
    if not text:
        return False
    return len(_CJK_RE.findall(text)) >= min_cjk


# 受控分类词表（LLM 只能从这些分类里选）
CATEGORIES = [
    "backend",    # 后端 / 服务端
    "frontend",   # 前端 / UI
    "ai_ml",      # AI / 机器学习
    "devops",     # 运维 / 基础设施
    "database",   # 数据库 / 存储
    "mobile",     # 移动端
    "security",   # 安全
    "tools",      # 开发工具 / 效率
    "other",      # 其他
]

# 分类中文说明（用于 prompt 和前端展示）
CATEGORY_LABELS = {
    "backend": "后端",
    "frontend": "前端",
    "ai_ml": "AI/机器学习",
    "devops": "运维",
    "database": "数据库",
    "mobile": "移动端",
    "security": "安全",
    "tools": "工具",
    "other": "其他",
}


def build_summary_prompt(items, kind: str = "repo") -> str:
    """构建批量结构化摘要的 prompt（kind: repo=仓库 / article=技术文章）"""
    lines = []
    for i, r in enumerate(items):
        # 参考文本（原始描述/正文）统一截断，避免超长内容稀释模型注意力
        desc = (r.get("summary") or r.get("description") or "").strip()[:220]
        lang = r.get("language") or ""
        topics = ", ".join((r.get("core_concepts") or [])[:4])
        lines.append(f"{i + 1}. {r['title']} | 描述: {desc} | 语言: {lang} | topics: {topics}")

    categories = "、".join(CATEGORIES)

    # 写作质量硬约束（对仓库和文章同时生效）：
    # - summary 是面向展示与向量检索的主文案，必须完整成句、自然收尾，且控制长度以便卡片三行内展示
    # - 提示词会告知模型该字段将用于向量化语义检索，促使它自然带上领域关键词
    quality_rules = "\n语言要求：除 category 外，所有字段必须用简体中文书写。输入里的英文描述/标题只是素材，必须阅读理解后**改写成中文**，严禁原样照抄或直接搬运英文句子。仅专有名词与业界通用技术术语（如 Kubernetes、Rust、Docker、API、RAG）可保留英文，其余一律中文化；只要出现成句的英文，就视为不合格。\n\n写作要求：summary 必须严格控制在 40~90 个中文字符之间（按中文字数计）。如果内容较多，请拆成 2 个短句并尽早使用句号收束，切忌写成一句超过 90 字的超长单句，也不要用分号硬凑；每一条文案语意完整、自然收尾，禁止用省略号或写到一半截断；禁止‘这是一个/该项目/本文’式的空洞开头。summary 会被用于向量化语义检索，请自然融入所属领域与技术关键词（如 RAG、向量数据库、k8s、编译器），不要写泛泛的套话。"

    if kind == "article":
        head = f"你是技术情报整理助手，负责把技术文章整理成结构化的中文技术卡片。\n\n下面有 {len(items)} 篇文章。请为每篇输出一个 JSON 对象，字段如下："
        schema_desc = f"""- category: 从 [{categories}] 里选最合适的一个
- summary: 完整中文简介，40~90 字，用 1~2 句讲清“文章主题/讲了什么 + 核心观点或价值 + 适合谁看”
- one_liner: 一句话主题概括，不超过 40 字，语意完整
- problem: 文章讨论的核心问题/观点，不超过 45 字
- tech_stack: 文章涉及的核心技术/关键词数组（2~5 个）
- highlights: 核心干货/亮点数组（2~3 个，每个不超过 18 字）
- use_cases: 适合谁看/典型场景数组（1~2 个）"""
        subject = "文章"
    else:
        head = f"你是技术情报整理助手，负责把 GitHub 仓库整理成结构化的中文技术卡片。\n\n下面有 {len(items)} 个仓库。请为每个仓库输出一个 JSON 对象，字段如下："
        schema_desc = f"""- category: 从 [{categories}] 里选最合适的一个
- summary: 完整中文简介，40~90 字，用 1~2 句讲清“这是什么 + 解决什么问题/痛点 + 亮点或适合谁”
- one_liner: 一句话定位，不超过 40 字，语意完整不截断
- problem: 解决什么问题/痛点，不超过 45 字
- tech_stack: 核心技术栈数组（2~5 个，如编程语言、框架、关键依赖）
- highlights: 核心亮点数组（2~3 个，每个不超过 18 字）
- use_cases: 典型适用场景数组（1~2 个）"""
        subject = "仓库"

    prompt = f"""{head}
{schema_desc}

只输出一个 JSON 数组，不要输出任何其他文字，格式：
[
  {{"category": "...", "summary": "...", "one_liner": "...", "problem": "...", "tech_stack": ["..."], "highlights": ["..."], "use_cases": ["..."]}},
  ...
]

{quality_rules}

{subject}列表：
{chr(10).join(lines)}"""
    return prompt

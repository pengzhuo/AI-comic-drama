"""视频提示词的「内容审核中性化」工具。

上游（火山 Doubao / Seedance）会对提交的提示词做文本内容审核，命中即用
``InputTextSensitiveContentDetected`` 以 4xx 拒绝整次提交。这条拒绝是**模糊**的：
同一段文字换个图片编号就可能通过，所以产线需要的是「可恢复的中性化降级」，
而不是把责任推回给用户重写文案。

本模块只做保守的**近义替换**：不改镜头、动作、道具、光线与一致性约束的语义，
只把湿身/贴身/身体部位、以及「喘息 + 身体起伏」这类高敏表述换成中性说法，
供三处复用：

* ``verification/video_consistency_gate.py``：强化提示词注入前先中性化，
  避免把视觉模型对画面的自由描述（例如「吊带背心与短裤」）原样写回提示词；
* ``generators/video_generator.py``：提交被文本审核拒绝后，用中性化提示词
  再抢救一次；
* ``chat/moderation_prompt_repair.py``：审核类失败后重投前改写提示词，
  ``neutralize_sensitive_elements`` 把可辨识证件/文字/暴露/血腥等画面元素
  一并中性化（这一类会动道具表述，所以单独一个函数）。
"""

from __future__ import annotations

import re

#: 上游文本内容审核的拒绝码（Volcengine Ark / Seedance 系列）。
TEXT_MODERATION_CODE = "InputTextSensitiveContentDetected"

#: 「输出视频（含版权）被拦」的标记。与 ``TEXT_MODERATION_CODE`` 的区别很关键：
#: 命中这些标记时提示词文字**已经过审**，被拦的是画面内容，所以单靠近义替换救不回，
#: 需要重新生成一版提示词（见 ``chat/moderation_prompt_repair.py``）。
OUTPUT_VIDEO_MARKERS: tuple[str, ...] = (
    "outputvideosensitivecontentdetected",
    "output video may contain sensitive",
    "copyright restrictions",
)

#: 先跑正则（能整段收掉啰嗦的修饰），再跑下面的字面替换。
_REGEX_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 「采用@图片1中呈现的湿透马尾、湿衣贴身的固有外观」整段收成一句中性引用。
    (
        re.compile(r"采用@图片(\d+)中呈现的[^，。；\n]{0,40}的固有外观"),
        r"其外观（服装、发型、妆容）一律以@图片\1为准",
    ),
)

#: 顺序敏感：长词在前，避免「吊带背心」被「吊带」先拆碎。
_LITERAL_RULES: tuple[tuple[str, str], ...] = (
    ("湿衣贴身", "衣物被雨水打湿"),
    ("湿透马尾", "被雨水打湿的马尾"),
    ("湿透的", "被雨水打湿的"),
    ("湿透", "淋湿"),
    ("湿衣", "被雨水打湿的衣物"),
    ("贴身", "自然贴合"),
    ("腰部高度", "中景机位"),
    ("腰际", "身侧"),
    ("腰部", "躯干"),
    ("吊带背心", "上衣"),
    ("吊带", "上衣"),
    ("背心", "上衣"),
    ("短裤", "长裤"),
    ("热裤", "长裤"),
    ("内衣", "衣物"),
    ("胸腹", "身前"),
    ("胸前", "手中"),
    ("胸口", "身前"),
    ("胸像特写", "近景特写"),
    ("臀部", "身后"),
    ("大腿", "腿部"),
    # —— 探针二分确认的触发簇 ——
    # 上游拦的不是单个词，而是「年轻女性 + 湿身 + 喘息/身体起伏」这一组合：
    # 单独回退「湿身质感」或「肩胛滑移」仍会被拒，单独回退「喘息/呼吸」或
    # 「身体起伏/发抖」即可放行。因此这几个语簇必须一并中性化。
    ("贴在皮肤上的湿发", "贴在鬓角的湿发"),
    ("湿发与衣物表面形成细密镜面反光", "发梢与外套表面带一层水光"),
    ("身体止不住地发抖", "肩头因冷意轻轻发颤"),
    ("身体一下一下地起伏", "呼吸逐渐平缓"),
    ("身体起伏由快到缓", "呼吸由快到缓"),
    ("与身体起伏、嘴唇开合同步", "与口型开合同步"),
    ("喘息虽未完全平复", "呼吸虽未完全平复"),
    ("急促的喘息", "急促的呼吸声"),
    ("微微喘息", "呼吸略微不稳"),
    ("喘息未匀", "呼吸未稳"),
    ("把呼吸压匀", "把呼吸放稳"),
    ("喘息", "呼吸"),
    ("肩胛沿着砖面缓缓向下滑移一段", "后背顺着砖墙轻轻下移"),
    ("肩胛沿砖面下移一段", "后背顺砖墙轻轻下移"),
    ("肩胛", "后背"),
)

#: ``neutralize_sensitive_elements`` 专用：先跑正则，再跑下面的字面替换。
_SENSITIVE_ELEMENT_REGEX_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 证件/工牌上的长数字（身份证号、工牌编号、银行卡号…）：画面里清晰可读时
    # 最容易被判「敏感信息」。只在本补救路径里遮蔽。
    (re.compile(r"(?<!\d)\d{6,}(?!\d)"), "〔编号略〕"),
)

#: ``neutralize_sensitive_elements`` 专用（长词在前）。规则只覆盖「画面元素」，
#: 不动镜头、动作、光线与一致性约束。
_SENSITIVE_ELEMENT_RULES: tuple[tuple[str, str], ...] = (
    # —— 可辨识的证件 / 单据 / 文件 / 文字 ——
    ("身份证特写", "手中卡片特写（画面不出现可辨识文字与号码）"),
    ("身份证正面", "本人卡片（画面不出现可辨识文字与号码）"),
    ("身份证", "本人卡片（画面不出现可辨识文字与号码）"),
    ("护照特写", "手中封套特写（画面不出现可辨识文字与号码）"),
    ("护照", "旅行卡片（画面不出现可辨识文字与号码）"),
    ("驾驶证", "本人卡片（画面不出现可辨识文字与号码）"),
    ("社保卡", "本人卡片（画面不出现可辨识文字与号码）"),
    ("工牌特写", "胸前挂牌特写（文字模糊不可辨识）"),
    ("员工卡", "胸前挂牌（文字模糊不可辨识）"),
    ("工牌", "胸前挂牌（文字模糊不可辨识）"),
    ("胸牌特写", "胸前挂牌特写（文字模糊不可辨识）"),
    ("胸牌", "胸前挂牌（文字模糊不可辨识）"),
    ("证件照", "本人近照（画面不出现可辨识文字与号码）"),
    ("证件", "本人卡片（画面不出现可辨识文字与号码）"),
    ("合同特写", "纸质材料特写（文字模糊不可辨识）"),
    ("合同", "纸质材料"),
    ("单据特写", "纸质材料特写（文字模糊不可辨识）"),
    ("单据", "纸质材料"),
    ("病历", "纸质材料"),
    ("文件特写", "纸质材料特写（文字模糊不可辨识）"),
    ("文件", "纸质材料"),
    ("屏幕上清晰可读的文字", "屏幕亮面（文字模糊不可辨识）"),
    ("屏幕上的文字清晰可读", "屏幕亮面（文字模糊不可辨识）"),
    ("屏幕上的文字", "屏幕亮面（文字模糊不可辨识）"),
    ("文字清晰可读", "文字模糊不可辨识"),
    ("文字清晰可见", "文字模糊不可辨识"),
    ("清晰可读的文字", "模糊到不可辨识的文字"),
    ("清晰可辨的文字", "模糊到不可辨识的文字"),
    ("清晰可辨的编号", "模糊到不可辨识的编号"),
    ("可辨识的编号", "模糊到不可辨识的编号"),
    ("编号清晰可见", "编号模糊不可辨识"),
    ("文字特写", "画面局部特写（不出现可辨识文字）"),
    # —— 血腥 / 伤口 ——
    ("血迹", "深色痕迹"),
    ("鲜血", "深色痕迹"),
    ("带血", "沾着深色痕迹"),
    ("血泊", "深色水痕"),
    ("伤口", "擦痕"),
    # —— 暴露 / 亲密 ——
    ("全裸", "衣着完整"),
    ("赤裸", "衣着完整"),
    ("半裸", "衣着完整"),
    ("裸露", "衣着完整"),
    ("吻戏", "靠近"),
    ("激吻", "相拥"),
    ("拥吻", "相拥"),
    ("接吻", "靠近"),
    ("亲吻", "靠近"),
    ("床戏", "同框"),
    ("抚摸", "轻触"),
    ("亲密接触", "靠近"),
)


def sanitize_prompt_for_moderation(text: str) -> str:
    """把提示词里的高敏表述换成中性说法；不改动其余语义。"""

    out = str(text or "")
    if not out:
        return ""
    for pattern, replacement in _REGEX_RULES:
        out = pattern.sub(replacement, out)
    for needle, replacement in _LITERAL_RULES:
        if needle in out:
            out = out.replace(needle, replacement)
    return re.sub(r"[ \t]{2,}", " ", out)


def is_text_moderation_rejection(text: str) -> bool:
    """判断一段上游错误文本是否属于「提示词文本被内容审核拦截」。"""

    return TEXT_MODERATION_CODE in str(text or "")


#: 提交层（``generators/video_generator.py``）已经用中性化提示词再抢救过一次、
#: 或者本地根本没有可中性化的表述时，会把这句写进错误文本。审核补救看到它就不必
#: 再做一次同样的确定性去敏——那一次必然还是被拒，只会白白烧掉一次生成。
#: 字面量与 video_generator 的报错文案一一对应，测试里有防漂移断言。
DESENSITIZE_ALREADY_TRIED_MARKERS: tuple[str, ...] = (
    "已尝试中性化改写仍被拒",
    "提示词已是中性文本，无可再降级",
)


def is_desensitize_exhausted(text: str) -> bool:
    """提交层是否已经试过（或判定无需）确定性去敏。"""

    raw = str(text or "")
    return any(marker in raw for marker in DESENSITIZE_ALREADY_TRIED_MARKERS)


def is_output_video_moderation_rejection(text: str) -> bool:
    """判断一段上游错误文本是否属于「输出画面（含版权）被内容审核拦截」。"""

    lowered = str(text or "").lower()
    return any(marker in lowered for marker in OUTPUT_VIDEO_MARKERS)


def neutralize_sensitive_elements(text: str) -> str:
    """完整的内容审核中性化：通用高敏表述 + 画面元素（审核补救专用）。

    先用 :func:`sanitize_prompt_for_moderation` 处理湿身/身体部位/喘息这类通用高敏
    表述，再把「可辨识证件/文字/编号、暴露、血腥」这些**画面元素**换成中性说法。

    之所以单独一个函数（而不是往 ``sanitize_prompt_for_moderation`` 里加规则）：
    本函数会改动**道具与画面元素**的表述（例如把「身份证特写」写成本人递出的卡片），
    而一致性强化提示词那条通路必须保留道具语义。只有「上游审核已经拒过一次」的
    补救路径才调用本函数。

    仍然遵守两条底线：不改镜头/动作/光线与一致性约束，也不删句子——只做近义替换。
    """

    out = sanitize_prompt_for_moderation(text)
    if not out:
        return ""
    for pattern, replacement in _SENSITIVE_ELEMENT_REGEX_RULES:
        out = pattern.sub(replacement, out)
    for needle, replacement in _SENSITIVE_ELEMENT_RULES:
        if needle in out:
            out = out.replace(needle, replacement)
    return re.sub(r"[ \t]{2,}", " ", out)

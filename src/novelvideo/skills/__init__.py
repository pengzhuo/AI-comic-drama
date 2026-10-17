"""内置技能与流程资源。

这个包同时承载两类内容：

* 导演工作台（Director World）的命令行技能脚本，如
  :mod:`novelvideo.skills.director_skill`；
* 随产品分发的**流程正文资源包**（非可执行代码），供各阶段注入到对应的
  用户提示词中。当前包含 :mod:`novelvideo.skills.ai_film_process`——即
  「通用AI影视制作流程包」六份正文。
"""

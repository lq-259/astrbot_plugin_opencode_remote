"""聊天-工作路由模块：判断用户消息是普通聊天还是代码工作任务"""
from dataclasses import dataclass
import json
import re


@dataclass
class RouteDecision:
    action: str           # "chat" | "confirm" | "opencode"
    reason: str           # 命中原因说明
    confidence: float     # 0.0 ~ 1.0
    rewritten_task: str   # 如果路由到 opencode， Original task text


class MessageRouter:
    """消息路由器：基于规则和关键词评分的轻量分类器"""

    def __init__(self, config: dict):
        router_cfg = config.get("router_config", {})
        self.enable_auto_route = router_cfg.get("enable_auto_route", False)
        self.mode = router_cfg.get("mode", "confirm")
        self.work_prefixes = [p.lower() for p in router_cfg.get("work_prefixes", ["/work", "/code"])]
        self.confirm_threshold = router_cfg.get("confirm_threshold", 0.65)
        self.auto_threshold = router_cfg.get("auto_threshold", 0.85)
        self.work_keywords = [kw.lower() for kw in router_cfg.get("work_keywords", [])]
        self.ignore_group_no_mention = router_cfg.get("ignore_group_messages_without_mention", True)
        self.enable_llm_intent = router_cfg.get("enable_llm_intent", False)
        self.intent_model = router_cfg.get("intent_model", "")

    def classify(self, raw_text: str, is_group: bool = False, is_mentioned: bool = False) -> RouteDecision:
        """判断一条消息应该 chat / confirm / opencode"""
        text = raw_text.strip()
        lower = text.lower()

        # 0. 群聊中未 @ 时直接放行（如果配置允许）
        if is_group and not is_mentioned and self.ignore_group_no_mention:
            return RouteDecision(
                action="chat",
                reason="群聊中未 @ 机器人",
                confidence=0.0,
                rewritten_task="",
            )

        # 1. 显式前缀路由（最高优先级，不受 mode 限制）
        for prefix in self.work_prefixes:
            if lower.startswith(prefix):
                task = text[len(prefix):].strip()
                return RouteDecision(
                    action="opencode",
                    reason=f"显式前缀命中：{prefix}",
                    confidence=1.0,
                    rewritten_task=task,
                )

        # 2. 自动路由关闭或模式为 off
        if not self.enable_auto_route or self.mode == "off":
            return RouteDecision(
                action="chat",
                reason="自动路由关闭",
                confidence=0.0,
                rewritten_task="",
            )

        # 3. 评分
        score, reasons = self._score(lower, text)

        # 4. 根据分数和模式决策
        if score >= self.auto_threshold and self.mode == "auto":
            return RouteDecision(
                action="opencode",
                reason="；".join(reasons),
                confidence=score,
                rewritten_task=text,
            )
        elif score >= self.confirm_threshold:
            return RouteDecision(
                action="confirm",
                reason="；".join(reasons),
                confidence=score,
                rewritten_task=text,
            )
        else:
            return RouteDecision(
                action="chat",
                reason="评分不足（{})".format("；".join(reasons) if reasons else "无命中关键词"),
                confidence=score,
                rewritten_task="",
            )

    async def classify_with_llm(self, raw_text: str, llm_callable) -> RouteDecision | None:
        """当规则评分不确定时，调用 LLM 做二次判断。

        Args:
            raw_text: 用户原始消息
            llm_callable: 异步 callable，接收 system_prompt 和 prompt，返回 LLMResponse 或字符串
        """
        system = (
            "你是一个消息意图分类器。判断用户消息是否是代码工作任务。"
            "只返回 JSON，不要任何解释。格式："
            '{"is_work_task": true/false, "confidence": 0.0~1.0, "reason": "简短原因"}'
        )
        prompt = f"用户消息：{raw_text}\n\n请判断这是否是代码工作任务。"
        try:
            resp = await llm_callable(system_prompt=system, prompt=prompt)
            # Parse JSON from response
            result_text = ""
            if hasattr(resp, "result_chain") and resp.result_chain:
                result_text = resp.result_chain.get_plain_text()
            elif hasattr(resp, "completion_text"):
                result_text = resp.completion_text
            elif hasattr(resp, "text"):
                result_text = resp.text
            else:
                result_text = str(resp)

            # Extract JSON
            json_match = re.search(r'\{.*?\}', result_text, re.DOTALL)
            if not json_match:
                return None
            result = json.loads(json_match.group())
            is_work = result.get("is_work_task", False)
            confidence = float(result.get("confidence", 0.0))
            reason = result.get("reason", "LLM 判断")

            if is_work and confidence >= self.auto_threshold:
                return RouteDecision(
                    action="opencode",
                    reason=f"LLM 判断：{reason}",
                    confidence=confidence,
                    rewritten_task=raw_text,
                )
            elif is_work and confidence >= self.confirm_threshold:
                return RouteDecision(
                    action="confirm",
                    reason=f"LLM 判断：{reason}",
                    confidence=confidence,
                    rewritten_task=raw_text,
                )
            else:
                return RouteDecision(
                    action="chat",
                    reason=f"LLM 判断非工作任务：{reason}",
                    confidence=confidence,
                    rewritten_task="",
                )
        except Exception:
            return None

    def _score(self, lower: str, original: str) -> tuple[float, list[str]]:
        """返回 (score, reasons_list)"""
        score = 0.0
        reasons = []
        text = lower

        # --- 否定词减分 ---
        negations = ["不要", "不用", "别", "不想", "不要写", "不用改", "不需要"]
        negation_penalty = 0
        for neg in negations:
            if neg in text:
                negation_penalty += 0.15
        negation_penalty = min(negation_penalty, 0.5)

        # --- 关键词加分 ---
        keyword_hits = []
        for kw in self.work_keywords:
            if kw in text:
                keyword_hits.append(kw)
                score += 0.08
        if keyword_hits:
            reasons.append(f"命中关键词：{', '.join(keyword_hits[:5])}")

        # --- 句式模式加分 ---
        patterns = [
            (r"帮我.*(修|改|写|加|实现|优化|重构|查|看)", "请求句式：帮我...", 0.12),
            (r"怎么.*(解决|修复|处理|写|实现)", "疑问句式：怎么...解决", 0.10),
            (r"能否.*(帮我|给我|写|改|实现)", "请求句式：能否...", 0.10),
            (r"请.*(修|改|写|加|实现|优化|重构)", "祈使句式：请...", 0.10),
            (r"(看看|看看怎么|查一下|检查一下|分析一下).*(代码|报错|问题|bug|错误)", "检查句式", 0.10),
            (r"(跑不通|跑不起来|报错|出错|失败|崩|挂).*[吗呢哪为什么怎么]", "故障排查句式", 0.15),
            (r"(ci|构建|build|测试|验证).*(失败|不过|报错)", "CI/构建句式", 0.12),
        ]

        for pat, reason_text, add in patterns:
            if re.search(pat, text):
                score += add
                reasons.append(reason_text)

        # --- 过度宽泛减分 ---
        if len(text) < 5:
            score -= 0.1
            reasons.append("消息过短")

        # 纯闲聊减分
        casual_words = ["怎么样", "在吗", "你好", "在干嘛", "今天", "天气", "心情", "吃饭", "睡觉"]
        casual_count = sum(1 for w in casual_words if w in text)
        if casual_count >= 2:
            score -= 0.2
            reasons.append("闲聊特征过多")

        # --- 最终分数裁剪 ---
        score = max(0.0, min(1.0, score - negation_penalty))
        return score, reasons

"""Extract and merge the current customer service task after intent recognition."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

_TASK_DOMAINS = {
    "order_status": "commerce", "logistics": "commerce", "refund": "commerce",
    "invoice": "commerce", "payment_issue": "commerce",
    "technical": "technical", "account": "account",
}
_DOMAIN_FIELDS = {
    "commerce": ("order_id", "amount", "date", "payment_channel"),
    "technical": ("error_code",),
    "account": (),
}
_REQUIRED_FIELDS = {
    "order_status": ("order_id",), "logistics": ("order_id",),
    "refund": ("order_id", "amount", "date", "payment_channel"),
    "payment_issue": ("order_id", "amount", "date", "payment_channel"),
    "invoice": ("order_id",),
}
_FIELD_LABELS = {
    "order_id": "订单号", "amount": "金额（元）", "date": "交易日期",
    "payment_channel": "支付渠道",
}
_TASK_CUES = {
    "logistics": r"物流|快递|运单|delivery|shipping",
    "refund": r"退款|退货|refund",
    "invoice": r"发票|invoice",
    "payment_issue": r"重复扣款|多扣|支付失败|扣费|payment failed",
    "technical": r"报错|错误码|(?:HTTP|报)\s*[45]\d{2}|登录失败|无法登录|闪退",
    "account": r"被盗|异常登录|重置密码|两步验证",
    "order_status": r"订单状态|发货了吗|查(?:下|一下)?订单|order status",
}
_ORDER_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}"
_ORDER_ALPHANUM = r"[A-Za-z]{1,12}-?\d[A-Za-z0-9_-]{0,27}"
_ORDER_LABEL = re.compile(
    rf"(?:订单(?:编号|号)|order\s*(?:id|number)|(?<![A-Za-z])订单)\s*[:#]?\s*"
    rf"(?:是|为)?\s*({_ORDER_TOKEN})(?![A-Za-z0-9_-])", re.I,
)
_ORDER_PAIR = re.compile(
    rf"(?:订单(?:编号|号)?|order\s*(?:id|number))\s*[:#]?\s*({_ORDER_TOKEN})"
    rf"\s*(?:和|与|、|/)\s*({_ORDER_TOKEN})(?![A-Za-z0-9_-])", re.I,
)
_BARE_ORDER = re.compile(rf"(?<![A-Za-z0-9_-])({_ORDER_ALPHANUM})(?![A-Za-z0-9_-])")
_AMOUNT_TOKEN = r"\d{1,10}(?:\.\d{1,2})?"
_AMOUNT = re.compile(rf"(?<![\w.+-])({_AMOUNT_TOKEN})\s*(?:元|块(?:钱)?)(?!\w)", re.ASCII)
_AMOUNT_LABEL = re.compile(
    rf"(?:金额(?:记错了|不对)?|实付|扣款)\s*(?:[, ]*(?:是|为|改成))?\s*[: ]*"
    rf"({_AMOUNT_TOKEN})(?![\d.])",
)
_DATE = re.compile(r"(?<!\d)(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}[日号]|今天|昨天|前天)(?!\d)")
_ERROR = re.compile(r"(?:HTTP|状态码|错误码|报错|报|返回)\s*[: ]*\s*([45]\d{2})(?!\d)", re.I)
_CHANNELS = {
    "微信": r"微信(?:支付)?|wechat\s*pay", "支付宝": r"支付宝|alipay",
    "银行卡": r"银行卡|信用卡|借记卡|储蓄卡",
    "银联": r"银联|unionpay", "现金": r"现金",
}
_SWITCH_ORDER = re.compile(
    rf"(?:换|改|切换)(?:成|到|个|一笔|一个)?(?:另一笔|另一个|其他|新)?订单"
    rf"|另(?:一笔|一个)订单|另一单|(?:换|改)(?:成|到)\s*{_ORDER_ALPHANUM}(?![A-Za-z0-9_-])"
)
_NEW_TASK = re.compile(r"换个问题|换个话题|新(?:的)?问题|另一个问题|说点别的|重新开始")
_END_TASK = re.compile(r"(?:不用了|先不查了|已经解决了|问题解决了|已解决|解决了|结束(?:当前)?(?:任务|问题)|取消(?:当前)?咨询)[!。 ]*")
_CONTINUATION = re.compile(r"还是不行|仍然不行|重登过了|重启过了|刚才那[笔单个]|这笔|这单|这个订单|之前那单|继续(?:查|处理|排查)|改成|记错了|不对")
_REMOVE = {
    "order_id": "订单号", "amount": "金额", "date": "(?:交易)?日期",
    "payment_channel": "支付渠道", "error_code": "错误码",
}
_WITHDRAW = r"(?:撤回|清除|删掉)(?:刚才的|之前的)?{label}|{label}(?:填错了|写错了|记错了|不对|不确定|作废)"


@dataclass(frozen=True, slots=True)
class BusinessTurn:
    state: dict[str, Any] | None
    active: bool = False
    reset: bool = False
    ambiguous_order: bool = False
    ask_fields: tuple[str, ...] = ()

    def entities(self) -> dict[str, list[str]]:
        if not self.active or not self.state:
            return {}
        return {key: [record["value"]] for key, record in self.state["fields"].items()}

    def prompt_data(self) -> dict[str, Any]:
        if not self.active or not self.state:
            return {}
        return {
            "domain": self.state["domain"], "task": self.state["task"],
            "subject": self.state["subject"],
            "fields": {key: values[0] for key, values in self.entities().items()},
        }

    def question(self) -> str:
        if self.ambiguous_order:
            return "请确认这次要处理的唯一订单号。"
        if self.ask_fields:
            return "请补充" + "、".join(_FIELD_LABELS[key] for key in self.ask_fields) + "。"
        return ""

    def after_reply(self, request_id: str) -> dict[str, Any] | None:
        if self.state is None or not self.ask_fields:
            return self.state
        return {
            **self.state,
            "pending": {"fields": list(self.ask_fields), "asked_request_id": request_id},
        }


def _read_state(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    task = raw.get("task")
    domain = _TASK_DOMAINS.get(task) if isinstance(task, str) else None
    if domain is None:
        return None
    fields = {}
    raw_fields = raw.get("fields")
    if isinstance(raw_fields, dict):
        for name in _DOMAIN_FIELDS[domain]:
            record = raw_fields.get(name)
            if (isinstance(record, dict) and record.get("source") == "user_message"
                    and isinstance(record.get("value"), str) and 0 < len(record["value"]) <= 40
                    and isinstance(record.get("request_id"), str)):
                fields[name] = {key: record[key] for key in ("value", "source", "request_id")}
    raw_pending = raw.get("pending")
    pending = None
    if isinstance(raw_pending, dict):
        requested = raw_pending.get("fields")
        asked = raw_pending.get("asked_request_id")
        if isinstance(requested, list) and isinstance(asked, str):
            missing = [key for key in _REQUIRED_FIELDS.get(task, ()) if key in requested and key not in fields]
            if missing:
                pending = {"fields": missing, "asked_request_id": asked}
    return _state(domain, task, fields, pending)


def _state(domain: str, task: str, fields: dict, pending: dict | None) -> dict[str, Any]:
    order = fields.get("order_id")
    subject = {"type": "order", "id": order["value"]} if order else None
    return {"domain": domain, "task": task, "subject": subject, "fields": fields, "pending": pending}


def _unique(matches: list[str]) -> str | None:
    values = set(matches)
    return next(iter(values)) if len(values) == 1 else None


def _transaction_date(value: str) -> str | None:
    if not re.match(r"\d{4}", value):
        return value
    year, month, day = map(int, re.findall(r"\d+", value))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _extract_fields(message: str, pending: set[str], *, commerce: bool) -> dict[str, list[str]]:
    if not commerce:
        codes = _ERROR.findall(message)
        if not codes and (match := re.fullmatch(r"(?:现在是|改成)?\s*([45]\d{2})[。! ]*", message)):
            codes = [match[1]]
        return {"error_code": codes}

    orders = _ORDER_LABEL.findall(message)
    orders.extend(value for pair in _ORDER_PAIR.findall(message) for value in pair)
    orders.extend(value for value in _BARE_ORDER.findall(message) if not re.fullmatch(r"HTTP[45]\d{2}", value, re.I))
    if not orders and "order_id" in pending:
        match = re.fullmatch(rf"(?:是\s*)?({_ORDER_TOKEN})[。! ]*", message)
        if match:
            orders = [match[1]]
    amounts = _AMOUNT.findall(message) + _AMOUNT_LABEL.findall(message)
    if not amounts and "amount" in pending and "order_id" not in pending:
        match = re.fullmatch(rf"(?:是\s*)?({_AMOUNT_TOKEN})[。! ]*", message)
        if match:
            amounts = [match[1]]
    # A labeled numeric order ID must not also become a payment amount.
    dates = [normalized for value in _DATE.findall(message) if (normalized := _transaction_date(value))]
    channels = [
        name for name, pattern in _CHANNELS.items()
        if re.search(pattern, message, re.I)
    ]
    return {
        "order_id": [value.upper() for value in orders if any(char.isdigit() for char in value)],
        "amount": amounts, "date": dates, "payment_channel": channels,
    }


def merge_business_state(previous: Any, message: str, intent: str | None, request_id: str) -> BusinessTurn:
    """Build a candidate state and the narrower view available to this reply."""
    old = _read_state(previous)
    message = unicodedata.normalize("NFKC", message).strip()
    if _END_TASK.fullmatch(message):
        return BusinessTurn(None, reset=old is not None)

    explicit_task = next((task for task, cue in _TASK_CUES.items() if re.search(cue, message, re.I)), None)
    detected_task = explicit_task or (intent if intent in _TASK_DOMAINS else None)
    switch_order = bool(_SWITCH_ORDER.search(message))
    new_task = bool(_NEW_TASK.search(message))
    old_task = old["task"] if old else None
    if switch_order and _TASK_DOMAINS.get(detected_task) != "commerce":
        detected_task = old_task if old and old["domain"] == "commerce" else "order_status"
    task = detected_task or (old_task if not new_task else None)
    if task is None:
        return BusinessTurn(None, reset=old is not None)

    domain = _TASK_DOMAINS[task]
    old_pending = old["pending"] if old and old_task == task and not (switch_order or new_task) else None
    pending = set(old_pending["fields"]) if old_pending else set()
    candidates = _extract_fields(message, pending, commerce=domain == "commerce") if domain != "account" else {}
    ambiguous_order = len(set(candidates.get("order_id", ()))) > 1
    updates = {key: value for key, values in candidates.items() if (value := _unique(values))}
    withdrawals = {
        field for field, label in _REMOVE.items()
        if field in _DOMAIN_FIELDS[domain] and re.search(_WITHDRAW.format(label=label), message)
    }
    active = bool(detected_task or updates or withdrawals or switch_order or new_task or ambiguous_order
                  or (old and _CONTINUATION.search(message)))
    if not active:
        return BusinessTurn(old)

    order = updates.get("order_id")
    previous_order = old["subject"]["id"] if old and old["subject"] else None
    order_changed = bool(order and previous_order and order != previous_order)
    order_withdrawn = "order_id" in withdrawals and not order
    reset = bool(new_task or switch_order or ambiguous_order or order_changed or order_withdrawn
                 or (old and old["domain"] != domain))
    fields = {} if reset or old is None else dict(old["fields"])
    if ambiguous_order:
        return BusinessTurn(_state(domain, task, {}, None), True, True, True, ("order_id",))
    for key in withdrawals:
        fields.pop(key, None)
    for key, value in updates.items():
        fields[key] = {"value": value, "source": "user_message", "request_id": request_id}

    # Ask only for a concrete case, not for a general policy question.
    missing = tuple(key for key in _REQUIRED_FIELDS.get(task, ()) if key not in fields)
    remaining = tuple(key for key in missing if key in pending) if not reset else ()
    unanswered = {"fields": list(remaining), "asked_request_id": old_pending["asked_request_id"]} if remaining else None
    concrete_case = bool(fields.keys() & {"order_id", "amount", "date"})
    ask = missing if (concrete_case or switch_order or order_withdrawn or (old_pending and not reset)) else ()
    return BusinessTurn(_state(domain, task, fields, unanswered), True, reset, ask_fields=ask)

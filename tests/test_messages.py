"""文案规范：无 emoji、语气词克制、行数上限、不出现元角分。"""

from __future__ import annotations

import re

from funhub import messages
from funhub.coins import CycleHit
from funhub.legacy import ImportReport

EMOJI_PATTERN = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u2190-\u21ff\u2700-\u27bf]",
)

#: 允许的排版符号：分隔、加赛串联、省略。它们落在 emoji 的码位区间里，但不是表情。
ALLOWED_SYMBOLS = "·→…"


def has_emoji(text: str) -> bool:
    return bool(EMOJI_PATTERN.search(text.translate({ord(char): None for char in ALLOWED_SYMBOLS})))


def duel_card(**overrides) -> str:
    payload = {
        "challenger": "龟甲",
        "target": "龟乙",
        "rolls_text": "90 : 10",
        "mode": "coin",
        "winner": "龟甲",
        "loser": "龟乙",
        "gain": 200,
        "penalty_note": "",
        "mute_failure": "",
        "immune_reason": "",
        "duration_text": "1 分钟",
    }
    payload.update(overrides)
    return messages.duel_card(**payload)


def roulette_hit(**overrides) -> str:
    payload = {
        "name": "龟乙",
        "shots": 2,
        "chambers": 6,
        "paid": 500,
        "penalty": 500,
        "muted": True,
        "mute_seconds": 60,
        "mute_blocked": False,
        "share_each": 250,
        "sharers": 2,
        "share_names": ("龟甲", "龟丙"),
    }
    payload.update(overrides)
    return messages.roulette_hit_card(**payload)


SAMPLES: dict[str, str] = {
    "checkin": messages.checkin_card(total=110, streak_days=3, coins_total=1250),
    "checkin_cycle": messages.checkin_card(
        total=460,
        streak_days=7,
        coins_total=1410,
        cycle_hits=(CycleHit(days=7, bonus=300),),
    ),
    "checkin_double_cycle": messages.checkin_card(
        total=2050,
        streak_days=210,
        coins_total=99999,
        cycle_hits=(CycleHit(days=7, bonus=300), CycleHit(days=30, bonus=1500)),
    ),
    "already": messages.checkin_already_card(coins_gain=110, streak_days=3, coins_total=1250),
    "auto_cycle": messages.auto_checkin_cycles(
        streak_days=7,
        cycle_hits=(CycleHit(days=7, bonus=300),),
    ),
    "profile": messages.profile_card(
        level=12,
        streak_days=3,
        total_checkins=42,
        coins=1250,
        rank=3,
        best_streak=15,
        cycle_days=7,
        days_to_cycle=4,
    ),
    "ranking": messages.ranking_card([(1, "龟甲", 30, 1250), (2, "龟乙", 12, 880)], by_coins=False),
    "ranking_empty": messages.ranking_card([], by_coins=True),
    "rank_of": messages.rank_card(name="龟甲", rank=3, level=30, coins=1250),
    "no_record": messages.no_record("龟甲"),
    "failed": messages.CHECKIN_FAILED,
    "duel_failed": messages.DUEL_FAILED,
    "duel": duel_card(),
    "duel_overtime": duel_card(rolls_text="50 : 50 → 90 : 10"),
    "duel_mute": duel_card(mode="coin_and_mute"),
    "duel_boosted": duel_card(
        gain=300,
        penalty_note="龟乙 存款多 1200，赔付 ×1.5",
    ),
    "duel_admin_boosted": duel_card(
        gain=400,
        penalty_note="龟乙 是管理员，赔付 ×1.7",
    ),
    "duel_immune": duel_card(
        mode="immune",
        gain=0,
        winner="龟乙",
        loser="龟甲",
        immune_reason="already_muted",
    ),
    "import": messages.import_summary(ImportReport(players_new=12, players_updated=3, checkins_inserted=456, coins_granted=57000)),
    "preview": messages.import_summary(ImportReport(dry_run=True, players_new=12, coins_granted=57000)),
    "roulette_start": messages.roulette_start_card(
        chambers=6,
        safe_reward=50,
        safe_reward_step=50,
        hit_penalty=500,
        misfire_percent=25,
        misfire_reward=500,
        mute_seconds=60,
    ),
    "roulette_start_quiet": messages.roulette_start_card(
        chambers=6,
        safe_reward=50,
        hit_penalty=0,
        mute_seconds=0,
        share_percent=0,
    ),
    "roulette_miss": messages.roulette_miss_card(shots=2, chambers=6, reward=100),
    "roulette_misfire": messages.roulette_misfire_card(
        name="龟乙",
        shots=6,
        chambers=6,
        reward=500,
    ),
    "roulette_hit": roulette_hit(),
    "roulette_hit_admin": roulette_hit(mute_blocked=True, muted=False),
    "roulette_hit_short": roulette_hit(paid=120, share_each=120),
    "roulette_hit_solo": roulette_hit(sharers=1, share_names=("龟甲",), share_each=500),
    "roulette_hit_crowd": roulette_hit(
        sharers=6,
        share_names=("甲", "乙", "丙", "丁", "戊", "己"),
        share_each=83,
    ),
    "roulette_rescue": messages.roulette_rescue_card(kind="rescued", name="龟乙"),
    "roulette_rescue_failed": messages.roulette_rescue_card(kind="failed", name="龟乙"),
    "roulette_rescue_cooldown": messages.roulette_rescue_card(
        kind="cooldown",
        name="龟乙",
        seconds=45,
    ),
    "judgment_cooldown": messages.roulette_judgment_card(kind="cooldown", seconds=45),
    "judgment": messages.roulette_judgment_card(
        kind="punished",
        names=("龟乙",),
        seconds=60,
    ),
    "judgment_many": messages.roulette_judgment_card(
        kind="punished",
        names=("龟乙", "龟丙"),
        seconds=60,
    ),
    "judgment_jam": messages.roulette_judgment_card(kind="fail"),
    "judgment_backfire": messages.roulette_judgment_card(
        kind="backfire",
        names=("龟甲",),
        seconds=60,
        actor_name="龟甲",
    ),
    "judgment_protected": messages.roulette_judgment_card(kind="protected"),
    "judgment_empty": messages.roulette_judgment_card(kind="no_one"),
    "judgment_wrong_target": messages.roulette_judgment_card(kind="not_punished"),
    "roulette_drunk": messages.roulette_drunk_card(
        victims=(("龟乙", 500), ("龟甲", 500)),
        shots=3,
        chambers=6,
        penalty=500,
        mute_seconds=60,
        any_muted=True,
        any_blocked=False,
        share_each=1000,
        share_names=("龟丙",),
    ),
    "roulette_drunk_poor": messages.roulette_drunk_card(
        victims=(("龟乙", 500), ("龟甲", 50)),
        shots=3,
        chambers=6,
        penalty=500,
        mute_seconds=60,
        any_muted=True,
        any_blocked=False,
    ),
    "hint": messages.command_hint("轮盘"),
    "hint_duel": messages.command_hint("决斗"),
}


def test_no_emoji_anywhere():
    for name, text in SAMPLES.items():
        assert not has_emoji(text), (name, text)


def test_no_wave_or_cat_particles():
    for name, text in SAMPLES.items():
        assert "～" not in text, name
        assert "喵" not in text, name
        assert "嗷" not in text, name


def test_no_money_units():
    """元角分只是内部标定口径，绝不进玩家可见文案。

    「元 / 角」一概不许出现；「分」只在"数字 + 分"这种金额写法上算违规
    —— 「分钟」是时长，「各分到」是动词。
    """
    for name, text in SAMPLES.items():
        assert "元" not in text, name
        assert "角" not in text, name
        assert not re.search(r"\d\s*分(?!钟)", text), name


def test_checkin_card_is_two_lines():
    assert len(SAMPLES["checkin"].splitlines()) == 2


def test_checkin_card_adds_one_line_per_cycle():
    assert len(SAMPLES["checkin_cycle"].splitlines()) == 3
    assert len(SAMPLES["checkin_double_cycle"].splitlines()) == 4
    assert SAMPLES["checkin_cycle"].splitlines()[-1] == "连签 7 天，额外 +300 币"


def test_already_card_is_two_lines():
    assert len(SAMPLES["already"].splitlines()) == 2
    assert SAMPLES["already"].splitlines()[0] == "今天已经签过啦"


def test_profile_card_is_three_lines():
    lines = SAMPLES["profile"].splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("等级 Lv.12")
    assert "本群第 3" in lines[1]


def test_ranking_card_has_title_and_one_line_per_player():
    lines = SAMPLES["ranking"].splitlines()
    assert lines[0] == "本群排行"
    assert len(lines) == 3
    assert lines[1] == "1. 龟甲 Lv.30 · 1250 币"
    assert SAMPLES["ranking_empty"] == "本群还没有人签到过"


def test_rank_and_no_record_cards():
    assert SAMPLES["rank_of"].splitlines()[0] == "龟甲 本群第 3"
    assert SAMPLES["no_record"] == "龟甲 暂无记录"


def test_import_summary_shows_backfill_and_preview_flag():
    assert SAMPLES["import"].startswith("导入完成：")
    assert "补发 57000 币" in SAMPLES["import"]
    assert SAMPLES["preview"].startswith("预览：")


def test_import_summary_mentions_skips_and_errors():
    report = ImportReport(
        players_new=1,
        checkins_inserted=2,
        checkins_skipped=3,
        coins_granted=250,
        errors=["坏记录"],
    )
    text = messages.import_summary(report)
    assert "跳过 3" in text
    assert "异常 1" in text


def test_duel_card_shapes():
    assert SAMPLES["duel"].splitlines() == [
        "决斗 · 龟甲 vs 龟乙",
        "90 : 10",
        "龟甲 胜，赢得 200 龟龟币",
    ]
    assert SAMPLES["duel_overtime"].splitlines()[1] == "50 : 50 → 90 : 10"
    assert SAMPLES["duel_mute"].splitlines()[-1] == "龟乙 被禁言 1 分钟"
    assert SAMPLES["duel_boosted"].splitlines()[-1] == "龟乙 存款多 1200，赔付 ×1.5"
    assert SAMPLES["duel_admin_boosted"].splitlines()[-1] == "龟乙 是管理员，赔付 ×1.7"
    assert SAMPLES["duel_immune"].splitlines()[-1] == "龟乙 胜，龟甲 正在禁言中，本次不罚"


def test_roulette_cards_shape():
    assert SAMPLES["roulette_miss"].splitlines() == ["空枪（2 / 6），+100 币"]
    assert SAMPLES["roulette_hit"].splitlines() == [
        "砰，龟乙 中弹（2 / 6）",
        "扣 500 币，禁言 1 分钟",
        "龟甲、龟丙 各分到 250 币",
    ]
    assert "（余额只有这么多）" in SAMPLES["roulette_hit_short"].splitlines()[1]
    assert SAMPLES["roulette_hit_admin"].splitlines()[1] == "扣 500 币（管理员禁不了言）"
    assert SAMPLES["roulette_hit_admin"].splitlines()[2] == "龟甲、龟丙 各分到 250 币"
    # 一个人时不用"各"；人多了收成"等 N 人"，别让一行名字压过卡片
    assert SAMPLES["roulette_hit_solo"].splitlines()[2] == "龟甲 分到 500 币"
    assert SAMPLES["roulette_hit_crowd"].splitlines()[2] == "甲、乙、丙、丁 等 6 人 各分到 83 币"


def test_roulette_start_card_spells_out_the_new_rules():
    assert SAMPLES["roulette_start"].splitlines() == [
        "轮盘装填完毕（6 个弹槽 · 1 颗子弹）",
        "空枪 +50 币，之后每枪再多 50",
        "子弹那一枪 25% 概率炸膛，开枪的人白拿 500 币",
        "中弹扣 500 币，全赔给开过枪的人，禁言 1 分钟",
        "发 /开枪 扣扳机",
    ]
    # 关掉赔付 / 禁言 / 炸膛时不留空话
    assert SAMPLES["roulette_start_quiet"].splitlines() == [
        "轮盘装填完毕（6 个弹槽 · 1 颗子弹）",
        "空枪 +50 币",
        "中弹扣 0 币",
        "发 /开枪 扣扳机",
    ]


def test_roulette_miss_and_misfire_cards():
    assert SAMPLES["roulette_miss"] == "空枪（2 / 6），+100 币"
    assert SAMPLES["roulette_misfire"] == "炸膛（6 / 6），龟乙 +500 币"


def test_drunk_card_shape():
    assert SAMPLES["roulette_drunk"].splitlines() == [
        "醉酒走火，龟乙、龟甲 一起中弹（3 / 6）",
        "各扣 500 币（共 1000），禁言 1 分钟",
        "龟丙 分到 1000 币",
    ]
    # 有人余额不够时改成说总额，别谎报"各扣 500"
    assert SAMPLES["roulette_drunk_poor"].splitlines()[1] == "共扣 550 币（按余额），禁言 1 分钟"


def test_cooldown_cards_shape():
    assert SAMPLES["roulette_rescue_cooldown"] == "刚救过了，再等 45 秒"
    assert SAMPLES["judgment_cooldown"] == "刚补过了，再等 45 秒"


def test_judgment_cards_shape():
    assert SAMPLES["judgment"] == "补了一枪，龟乙 又被禁言 1 分钟"
    assert SAMPLES["judgment_many"] == "补了一枪，龟乙、龟丙 各又被禁言 1 分钟"
    assert SAMPLES["judgment_jam"] == "卡壳了，这一枪没补上"
    assert SAMPLES["judgment_backfire"] == "枪口歪了，龟甲 自己又被禁言 1 分钟"
    assert SAMPLES["judgment_protected"] == "补不动，对方是管理员（禁不了言）"
    assert SAMPLES["judgment_empty"] == "没有可以补枪的人（名单里的人都出来了）"
    assert SAMPLES["judgment_wrong_target"] == "只能补本局中弹被禁言的人"


def test_hint_cards_shape():
    assert SAMPLES["hint"] == "指令是 /轮盘"
    assert SAMPLES["hint_duel"] == "指令是 /决斗，记得 @ 上对手"


def test_roulette_cards_stay_within_five_lines():
    for name in (
        "roulette_start",
        "roulette_start_quiet",
        "roulette_miss",
        "roulette_misfire",
        "roulette_hit",
        "roulette_hit_admin",
        "roulette_hit_short",
        "roulette_hit_solo",
        "roulette_hit_crowd",
        "roulette_rescue",
        "roulette_rescue_failed",
    ):
        assert len(SAMPLES[name].splitlines()) <= 5, name


def test_duel_card_stays_within_five_lines():
    for name in (
        "duel",
        "duel_overtime",
        "duel_mute",
        "duel_boosted",
        "duel_admin_boosted",
        "duel_immune",
    ):
        assert len(SAMPLES[name].splitlines()) <= 5, name


def test_duel_card_never_says_tie():
    """平局不存在：文案里不该再出现平局。"""
    for name, text in SAMPLES.items():
        assert "平局" not in text, name

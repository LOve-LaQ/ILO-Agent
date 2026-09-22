# Test Eval Gate - 输出质量评测门禁
"""改 prompt 后先跑 `pytest -m eval`，分数掉了不许合并。

本模块整块打 `@pytest.mark.eval`：
- 默认 `pytest` 会通过 pytest.ini 的 `addopts = -m "not eval"` 自动 deselect，
  保证「默认零 LLM 调用」这条既有契约不被破坏；
- 只有显式 `pytest -m eval` 才真正调用 LLM（会花钱）。

断言口径（双层，跑**全量**数据集，样本量小会让单条噪声盖过信号）：
- 性质硬线：`chinese` / `faithfulness` 两个维度不得跌破 3.0 —— 这两项一旦退化
  就是产出直接不可用，是绝对底线，不随基线浮动
- 回归线：总均分与其余各维度 >= 基线对应值 - 0.4（0.4 由连跑实测 σ≈0.0~0.19，取 ~2σ）
- 裁判不可用 / 尚无基线 / 无可计分样本 -> skip（而不是失败），把「环境没配好」
  与「质量真的退化了」区分开
"""

import asyncio

import pytest

pytestmark = pytest.mark.eval

# 相对回归线容差：连跑 3 次实测各维度 σ≈0.0~0.19（裁判已固定 temperature=0），
# 取 ~2σ 的 0.4；再小会因单次波动假红【已实测】
_REGRESSION_TOLERANCE = 0.4
# 性质维度绝对硬线：语言正确性(chinese) 与 无编造(faithfulness) 跌破即不可用
_HARD_FLOOR = 3.0
_HARD_FLOOR_DIMS = ("chinese", "faithfulness")


@pytest.fixture(autouse=True)
def _force_llm_fallback():
    """覆盖 tests/conftest.py 中的同名 autouse 夹具。

    conftest 为了让契约测试离线可复现，强制把 LLM 置为不可用（`LLM_AVAILABLE=False`
    且 `llm=None`）——但那样 `create_llm("deepseek")` 永远返回 None，裁判恒不可用，
    eval 用例只会永远 skip，等于门禁失效。

    这里用**同名夹具**覆盖它（pytest 就近优先），让 eval 用例真的能调模型；
    由于整个模块默认被 deselect，这个覆盖在默认 `pytest` 下不会产生任何调用。
    """
    yield


def _run_dataset(name: str):
    """跑**全量**数据集；裁判不可用时 skip。

    刻意不用小样本：单条换一次 judge 分，维度均分就跳 0.2+，噪声会盖过信号。
    全量约 20 来条 × 1 次 judge 调用，成本可忽略。
    """
    from evals.judge import JudgeUnavailableError
    from evals.runner import run_dataset

    try:
        return asyncio.run(run_dataset(name))
    except JudgeUnavailableError as e:
        pytest.skip(f"裁判模型不可用：{e}")


def _assert_gate(name: str) -> None:
    from evals.judge import JUDGE_DIMENSIONS
    from evals.runner import load_baseline

    base = load_baseline().get(name) or {}
    base_overall = base.get("overall")
    if base_overall is None:
        pytest.skip(
            f"尚无 {name} 基线，先跑 "
            f"`python -m evals.runner --dataset {name} --update-baseline`"
        )

    result = _run_dataset(name)
    if not result["scored"]:
        pytest.skip("没有可计分样本（生成或判分全部失败，多半是未配置模型 Key）")

    assert result["overall"] >= base_overall - _REGRESSION_TOLERANCE, (
        f"{name} 总均分 {result['overall']} 低于基线 {base_overall}"
        f"（回归容忍 {_REGRESSION_TOLERANCE}）"
    )

    base_dims = base.get("dimension_means") or {}
    for dim in JUDGE_DIMENSIONS:
        mean = result["dimension_means"][dim]
        if mean is None:
            continue
        if dim in _HARD_FLOOR_DIMS:
            # 性质维度只卡绝对硬线：judge 对长文本的 faithfulness 打分离散度较大
            # （实测 digest σ≈0.24），再叠一条 0.3 回归线会频繁假红。
            assert mean >= _HARD_FLOOR, (
                f"{name} 的性质维度 {dim} 跌破硬线 {_HARD_FLOOR}：{mean}"
            )
            continue
        base_dim = base_dims.get(dim)
        if base_dim is not None:
            assert mean >= base_dim - _REGRESSION_TOLERANCE, (
                f"{name} 的 {dim} 较基线 {base_dim} 下降超过 {_REGRESSION_TOLERANCE}：{mean}"
            )


def test_eval_gate_summary():
    _assert_gate("summary")


def test_eval_gate_digest():
    _assert_gate("digest")

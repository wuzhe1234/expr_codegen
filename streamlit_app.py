import inspect

import streamlit as st
import sympy
from black import format_str, Mode
from loguru import logger
from streamlit_ace import st_ace
from sympy import numbered_symbols, Eq

import expr_codegen
from expr_codegen.expr import replace__ts_sum__to__ts_mean, replace__repeat, replace__one_mul, replace__ts_xxx_1, replace__ts_delay__to__ts_delta, string_to_exprs
from expr_codegen.tool import ExprTool

# 引用一次，防止被IDE格式化。因为之后表达式中可能因为==被换成了Eq
_ = Eq

st.set_page_config(page_title='Expr Codegen', layout="wide")

with st.sidebar:
    st.subheader("配置参数")

    date_name = st.text_input('日期字段名', 'date')
    asset_name = st.text_input('资产字段名', 'asset')

    # 生成代码
    style = st.radio('代码风格', ('polars', 'pandas'))
    if style == 'polars':
        from expr_codegen.polars.code import codegen
    else:
        from expr_codegen.pandas.code import codegen

    st.subheader("优化")
    is_pre_opt = st.checkbox('事前`表达式`化简', True)
    # TODO: 好像这个还有问题等有空再改
    is_back_opt = st.checkbox('事后`整列分组`向前合并', True)
    is_chain_opt = st.checkbox('事后`首尾接龙`向前合并', True)

    st.subheader("关于")
    st.markdown(f"""[Github仓库](https://github.com/wukan1986/expr_codegen)

[问题反馈](http://github.com/wukan1986/expr_codegen/issues)

作者: wukan

声明：
1. 本站点不存储用户输入的表达式，安全保密可放心
2. 生成的代码可能有错，发现后请及时反馈

version: {expr_codegen.__version__}
    """)


st.title('表达式转译代码')

with st.expander(label="预定义的**因子**和**算子**"):
    st.write('如缺算子，可以在issue中申请添加，或下载代码进行二次开发')

    # import examples.sympy_define
    # from examples.sympy_define import *

    # 本可以不用写这么复杂，但为了证明可以动态加载和执行，所以演示一下
    module = __import__('examples.sympy_define', fromlist=['*'])

    codes = inspect.getsource(module)
    st.code(codes)
    # 执行
    exec(codes)

st.subheader('自定义因子')
factors = st.text_area(label='可覆写已定义因子', value="""OPEN, HIGH, LOW, CLOSE, VOLUME, AMOUNT,
RETURNS, VWAP, CAP,
ADV5, ADV10, ADV15, ADV20, ADV30, ADV40, ADV50, ADV60, ADV81, ADV120, ADV150, ADV180,
SECTOR, INDUSTRY, SUBINDUSTRY,""")

st.subheader('自定义表达式')
exprs_src = st_ace(value="""# 请在此添加表达式，`=`右边为表达式，`=`左边为输出因子名。
alpha_003=-1 * ts_corr(cs_rank(OPEN), cs_rank(VOLUME), 10)
alpha_006=-1 * ts_corr(OPEN, VOLUME, 10)
alpha_101=(CLOSE - OPEN) / ((HIGH - LOW) + 0.001)

LABEL_OO_1=ts_delay(OPEN, -2)/ts_delay(OPEN, -1)-1 # 第二天开盘交易
LABEL_OO_2=ts_delay(OPEN, -3)/ts_delay(OPEN, -1)-1 # 第二天开盘交易，持有二天
LABEL_CC_1=ts_delay(CLOSE, -1)/CLOSE-1 # 每天收盘交易
""",
                   language="python",
                   auto_update=True,
                   )

if st.button('代码生成'):
    st.write('请点击**代码区 右上角 图标按钮**进行复制')

    # 自定义注册到全局变量
    sympy.var(factors)

    # eval处理，转成字典
    exprs_src = string_to_exprs(exprs_src, globals())

    if is_pre_opt:
        logger.info('事前 表达式 化简')
        # Alpha101中大量ts_sum(x, 10)/10, 转成ts_mean(x, 10)
        exprs_src = {k: replace__ts_sum__to__ts_mean(v) for k, v in exprs_src.items()}
        # alpha_031中大量cs_rank(cs_rank(x)) 转成cs_rank(x)
        exprs_src = {k: replace__repeat(v) for k, v in exprs_src.items()}
        # 1.0*VWAP转VWAP
        exprs_src = {k: replace__one_mul(v) for k, v in exprs_src.items()}
        # 将部分参数为1的ts函数进行简化
        exprs_src = {k: replace__ts_xxx_1(v) for k, v in exprs_src.items()}
        # ts_delay转成ts_delta
        exprs_src = {k: replace__ts_delay__to__ts_delta(v) for k, v in exprs_src.items()}

    # TODO: 一定要正确设定时间列名和资产列名，以及表达式识别类
    tool = ExprTool(date=date_name, asset=asset_name)

    logger.info('表达式 抽取 合并')
    exprs_dst, syms_dst = tool.merge(**exprs_src)

    logger.info('提取公共表达式')
    tool.cse(exprs_dst, symbols_repl=numbered_symbols('_x_'), symbols_redu=exprs_src.keys())

    logger.info('生成有向无环图')
    exprs_ldl = tool.dag(False)

    logger.info('分组优化')
    exprs_ldl.optimize(back_opt=is_back_opt, chain_opt=is_chain_opt)

    logger.info('代码生成')
    codes = codegen(exprs_ldl, exprs_src, syms_dst, filename='template.py.j2')

    res = format_str(codes, mode=Mode(line_length=500))
    st.code(res, language='python')

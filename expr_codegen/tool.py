from black import Mode, format_str
from sympy import simplify, cse, symbols, numbered_symbols

from expr_codegen.expr import get_current_by_prefix, get_children, replace__ts_sum__to__ts_mean, replace__repeat, replace__one_mul, replace__ts_xxx_1, replace__ts_delay__to__ts_delta
from expr_codegen.model import dag_start, dag_end, dag_middle


class ExprTool:

    def __init__(self, date: str, asset: str):
        """指定分组时用到的时间和资产的字段名

        Parameters
        ----------
        date: str
            日期时间字段名
        asset: str
            资产字段名
        """
        self.date = date
        self.asset = asset
        self.get_current_func = get_current_by_prefix
        self.get_current_func_kwargs = {}
        self.exprs_dict = {}
        self.exprs_names = []

    def set_current(self, func, **kwargs):
        self.get_current_func = func
        self.get_current_func_kwargs = kwargs

    def extract(self, expr):
        """抽取分割后的子公式

        Parameters
        ----------
        expr
            单表达式

        Returns
        -------
        表达式列表

        """
        # 抽取前先化简
        expr = simplify(expr)

        exprs = []
        syms = []
        get_children(self.get_current_func, self.get_current_func_kwargs,
                     expr,
                     output_exprs=exprs, output_symbols=syms,
                     date=self.date, asset=self.asset)
        # print('=' * 20, expr)
        # print(exprs)
        return exprs, syms

    def merge(self, **kwargs):
        """合并多个表达式

        1. 先抽取分割子公式
        2. 合并 子公式+长公式，去重

        Parameters
        ----------
        kwargs
            表达式字典

        Returns
        -------
        表达式列表
        """
        exprs_syms = [self.extract(v) for v in kwargs.values()]
        exprs = []
        syms = []
        for e, s in exprs_syms:
            exprs.extend(e)
            syms.extend(s)

        syms = sorted(set(syms), key=syms.index)
        # 如果目标有重复表达式，这里会混乱
        exprs = sorted(set(exprs), key=exprs.index)
        exprs = exprs + list(kwargs.values())

        # print(exprs)

        return exprs, syms

    def reduce(self, repl, redu):
        """减少中间变量数量，有利用减少内存占用"""

        exprs_dict = {}

        # 不做改动，直接生成
        for variable, expr in repl:
            exprs_dict[variable] = simplify(expr)
        for variable, expr in redu:
            exprs_dict[variable] = simplify(expr)

        return exprs_dict

    def cse(self, exprs, symbols_repl=None, symbols_redu=None):
        """多个子公式+长公式，提取公共公式

        Parameters
        ----------
        exprs
            表达式列表
        symbols_repl
            中间字段名迭代器
        symbols_redu
            最终字段名列表

        Returns
        -------
        graph_dag
            依赖关系的有向无环图
        graph_key
            每个函数分组用key
        graph_exp
            表达式

        """
        self.exprs_names = list(symbols_redu)

        repl, redu = cse(exprs, symbols_repl, optimizations="basic")
        outputs_len = len(symbols_redu)

        new_redu = []
        symbols_redu = iter(symbols_redu)
        for expr in redu[-outputs_len:]:
            # 可能部分表达式只在之前出现过，后面完全用不到如，ts_rank(ts_decay_linear(x_147, 11.4157), 6.72611)
            variable = next(symbols_redu)
            variable = symbols(variable)
            new_redu.append((variable, expr))

        self.exprs_dict = self.reduce(repl, new_redu)

        # with open("exprs.pickle", "wb") as file:
        #     pickle.dump(exprs_dict, file)

        return self.exprs_dict

    def dag(self, fast):
        """生成DAG"""
        G = dag_start(self.exprs_dict, self.get_current_func, self.get_current_func_kwargs, self.date, self.asset)
        if not fast:
            G = dag_middle(G, self.exprs_names, self.get_current_func, self.get_current_func_kwargs, self.date, self.asset)
        return dag_end(G)

    def all(self, exprs_src, style: str = 'polars', template_file: str = 'template.py.j2', fast: bool = False):
        """功能集成版，将几个功能写到一起方便使用

        Parameters
        ----------
        exprs_src: dict
            表达式字典
        style: str
            代码风格。可选值 ('polars', 'pandas')
        template_file: str
            根据需求可定制模板
        fast:bool
            快速模式。将跳过表达式化简。生成代码的分组重排，以及代码格式化这类为人类体验而服务的功能
        Returns
        -------
        代码字符串

        """
        assert style in ('polars', 'pandas')

        if not fast:
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

        # 子表达式在前，原表式在最后
        exprs_dst, syms_dst = self.merge(**exprs_src)

        # 提取公共表达式
        self.cse(exprs_dst, symbols_repl=numbered_symbols('_x_'), symbols_redu=exprs_src.keys())
        # 有向无环图流转
        exprs_ldl = self.dag(fast)

        if not fast:
            # 因为遗传算法中的表达式是单个输入，所以没有必要优化
            exprs_ldl.optimize(back_opt=True, chain_opt=True)

        if style == 'polars':
            from expr_codegen.polars.code import codegen
        else:
            from expr_codegen.pandas.code import codegen

        codes = codegen(exprs_ldl, exprs_src, syms_dst, filename=template_file)

        if not fast:
            # 格式化。在遗传算法中没有必要
            codes = format_str(codes, mode=Mode(line_length=1000))

        return codes

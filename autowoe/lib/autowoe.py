import collections
import warnings
from collections import OrderedDict
from copy import deepcopy
from multiprocessing import Pool
from typing import Union, Dict, List, Hashable, Optional, Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .cat_encoding.cat_encoding import CatEncoding
from .logger import get_logger
from .optimizer.optimizer import TreeParamOptimizer
from .pipelines.pipeline_homotopy import HTransform
from .pipelines.pipeline_smallnans import SmallNans
from .selectors.selector_first import nan_constant_selector, feature_imp_selector
from .selectors.selector_last import Selector
from .types_handler.types_handler import TypesHandler
from .utilities.cv_split_f import cv_split_f
from .utilities.refit import refit_reg, refit_simple
from .utilities.sql import get_sql_query
from .woe.woe import WoE

logger = get_logger(__name__)

SplitType = Optional[Union[np.ndarray, List[float], Dict[int, int]]]


def get_monotonic_constr(name: str, train: pd.DataFrame, target: str):
    df = train[[target, name]].dropna()
    try:
        auc = roc_auc_score(df[target].values, df[name].values)
    except (ValueError, TypeError):
        return '0'

    return str(int(np.sign(auc - 0.5)))


_small_nan_set = {"__NaN_0__", "__NaN_maxfreq__", "__NaN_maxp__", "__NaN_minp__",
                  "__Small_0__", "__Small_maxfreq__", "__Small_maxp__", "__Small_minp__"}

_nan_set = {"__NaN_0__", "__NaN__", "__NaN_maxfreq__", "__NaN_maxp__", "__NaN_minp__"}


class AutoWoE:
    """Implementation of Logistic regression with WoE transformation."""

    @property
    def weights(self):
        return self._weights

    @property
    def intercept(self):
        return self._intercept

    @property
    def p_vals(self):
        return self._p_vals

    def __init__(self,
                 interpreted_model: bool = True,
                 monotonic: bool = False,
                 max_bin_count: int = 5,
                 select_type: Optional[int] = None,
                 pearson_th: float = 0.9,
                 auc_th: float = .505,
                 vif_th: float = 5.,
                 imp_th: float = 0.001,
                 th_const: Union[int, float] = 0.005,
                 force_single_split: bool = False,
                 th_nan: Union[int, float] = 0.005,
                 th_cat: Union[int, float] = 0.005,
                 woe_diff_th: float = 0.01,
                 min_bin_size: Union[int, float] = 0.01,
                 min_bin_mults: Sequence[float] = (2, 4),
                 min_gains_to_split: Sequence[float] = (0.0, 0.5, 1.0),
                 auc_tol: float = 1e-4,
                 cat_alpha: float = 1,
                 cat_merge_to: str = "to_woe_0",
                 nan_merge_to: str = 'to_woe_0',
                 oof_woe: bool = False,
                 n_folds: int = 6,
                 n_jobs: int = 10,
                 l1_grid_size: int = 20,
                 l1_exp_scale: float = 4,
                 imp_type: str = "feature_imp",
                 regularized_refit: bool = True,
                 p_val: float = 0.05,
                 debug: bool = False,
                 **kwargs
                 ):
        """
        Инициализация основных гиперпараметров алгоритма построения интерпретиремой модели

        Args:
        interpreted_model: bool
            Флаг интерпретируемости модели
        monotonic: bool
            Глобальное условие на монотонность. Если True, то будут построены только монотонные биннинги
            В метод .fit можно передать значения, изменяющие это условие отдельно для каждой фичи
        max_bin_count: int
            Глобальное ограничение на количество бинов. Может быть переписано для каждой фичи в .fit
        select_type: None ot int
            Тип первичного отбора признаков, если число, то
            оставляем только столько признаков (самых лучших по feature_importance).
            Если None оставлям те, у которых feature_importance больше 0.
        pearson_th:  0 < pearson_th < 1
            Трешхолд отбора признаков по корреляции. Будут отброшены все признаки,
            у которых коэффициент корреляции больше по модулю pearson_th.
        auc_th: .5 < auc_th < 1
            Трешхолд отбора признаков по одномерному AUC. WOE c AUC < auc_th будут отброшены
        vif_th: vif_th > 0
            Трешхолд отбора признаков по VIF. Признаки с VIF > vif_th итеративно отбрасываются по одному
            затем VIF пересчитывается, пока VIF всех не будет менее vif_th
        imp_th: real >= 0
            Трешхолд для отбора признаков по features importance
        th_const:
            Трешхолд в заключении о том, что признак константный.
            Если число валидных значений больше трешхолда, то колонка не константная (int)
            В случае указания float, число валидных значений будет определяться как размер_выборки * th_const
        force_single_split: bool
            В параметрах дерева можно задавать минимальное число наблюдений в листе. Таким образом,
            для каких то фичей станет невозможно сделать разбиение на хотя бы 2 бина. Указав force_single_split=True можно
            сдалть так, что для такой фичи создастся 1 сплит в случае если минимальный бин будет размером более чем th_const
        th_nan: int >= 0
            Трешхолд в заключении о том, что нужен подсчет WoE значений на None
        th_cat: int >= 0
            Трешхолд в заключении о том, какие категории считать маленькими
        woe_diff_th: float = 0.01
            Возмодность смеджить наны и редкие категории с каким-то бином,
            если разница в вое менее woe_diff_th
        min_bin_size: int > 1, 0 < float < 1
            Минимальный размер бина при разбиении
        min_bin_mults: list of floats > 1
            Существует заданный минимальный размер бина.
            Здесь можно указать лист, чтобы проверить - не работают ли лучше большие значения, пример [2, 4]
        min_gains_to_split: list of floats >= 0
            Значения min_gain_to_split которые будут перебраны для поиска лучшего сплита
        auc_tol: 1e-5 <= auc_tol <=1e-2
            Чувствительность к AUC. Считаем, что можем пожертвовать auc_tol качества от максимального,
            чтобы сделать модель проще
        cat_alpha: float > 0
            Регуляризатор для кодирования категорий
        cat_merge_to: str
            Способ заполенния WoE значений на тестовой выборке для категорий, которых не было в обучающей выборке
            Значения - 'to_nan', 'to_woe_0', 'to_maxfreq', 'to_maxp', 'to_minp'
        nan_merge_to: str
            Способ заполнения WoE значений на тестовой выборке для вещественных нанов, в случае, если они не попали
            в свою группу Значения - 'to_woe_0', 'to_maxfreq', 'to_maxp', 'to_minp'
        oof_woe: bool
            Использовать oof кодирование для WOE, либо по классике
        n_folds: int
            Количество фолдов для отбора/кодирования итд ..
        n_jobs: int > 0
            Число используемых ядер
        l1_grid_size: int > 0
            Размер сетки в l1 регуляризации
        l1_exp_scale: float > 1
            Шкала сетки в l1 регуляризации
        imp_type: str
            Тип важности признаков. Доступны feature_imp и perm_imp.
           По нему происходит сортировка признаков как на первой, так и на заключительной стадии отбора
        regularized_refit: bool
            Использовать регуляризацию в момент рефита модели. Иначе стат модель
        p_val: 0 < p_val <= 1
            В случае построения стат модели делать backward отбор до тех пор, пока все pvalues коэф модели
            не будут меньше p_val
        debug: bool
            Дебаг режим
            **kwargs:
        """

        assert cat_merge_to in ['to_nan', 'to_woe_0', 'to_maxfreq', 'to_maxp', 'to_minp'], \
            "Value for cat_merge_to is invalid. Valid are 'to_nan', 'to_small', 'to_woe_0', 'to_maxfreq', 'to_maxp', 'to_minp'"

        assert nan_merge_to in ['to_woe_0', 'to_maxfreq', 'to_maxp', 'to_minp'], \
            "Value for nan_merge_to is invalid. Valid are 'to_woe_0', 'to_maxfreq', 'to_maxp', 'to_minp'"

        self._params = {

            'interpreted_model': interpreted_model,
            'monotonic': monotonic,
            'max_bin_count': max_bin_count,
            'select_type': select_type,
            'pearson_th': pearson_th,
            'auc_th': auc_th,
            'vif_th': vif_th,
            'imp_th': imp_th,
            'min_bin_mults': min_bin_mults,
            'min_gains_to_split': min_gains_to_split,
            'force_single_split': force_single_split,
            'auc_tol': auc_tol,
            'cat_alpha': cat_alpha,
            'cat_merge_to': cat_merge_to,
            'nan_merge_to': nan_merge_to,
            'oof_woe': oof_woe,
            'n_folds': n_folds,
            'n_jobs': n_jobs,
            'l1_grid_size': l1_grid_size,
            'l1_exp_scale': l1_exp_scale,

            'imp_type': imp_type,
            'population_size': None,
            'regularized_refit': regularized_refit,
            'p_val': p_val,
            'debug': debug,

            'th_const': th_const,
            'th_nan': th_nan,
            'th_cat': th_cat,
            'woe_diff_th': woe_diff_th,
            'min_bin_size': min_bin_size

        }
        for deprecated_arg, new_arg in zip(['l1_base_step', 'l1_exp_step', 'population_size', 'feature_groups_count'],
                                           ['l1_grid_size', 'l1_exp_scale', None, None]):

            if deprecated_arg in kwargs:
                msg = 'Parameter {0} is deprecated.'.format(deprecated_arg)
                if new_arg is not None:
                    msg = msg + ' Value will be set to {0} parameter, but exception will be raised in future.'.format(new_arg)
                    self._params[new_arg] = kwargs[deprecated_arg]
                warnings.warn(msg, DeprecationWarning, stacklevel=2)

        self.woe_dict = None
        self.train_df = None
        self.split_dict = None  # словарь со сплитами для каждого признкака
        self.target = None  # целевая переменная
        self.clf = None  # модель лог регрессии
        self.features_fit = None  # Признаки, которые прошли проверку Selector + информация о лучшей итерации Result
        self._cv_split = None  # Словарь с индексами разбиения на train и test
        self._small_nans = None

        self._private_features_type = None
        self._public_features_type = None

        self._weights = None
        self._intercept = None
        self._p_vals = None

        self.feature_history = None

    @property
    def features_type(self):
        """
        Геттер исходного набора признаков и их типов

        Returns:

        """
        return self._public_features_type

    @property
    def private_features_type(self):
        """
        Геттер внутренней типизации признаков

        Returns:

        """
        return self._private_features_type

    def get_split(self, feature: Hashable):
        """
        Геттер внутренностей разбиения на бины

        Args:
            feature:

        Returns:

        """
        return self.woe_dict[feature].split

    def get_woe(self, feature_name: Hashable):
        """
        Геттер WoE значений

        Args:
            feature_name: Название признака

        Returns:

        """
        if self.private_features_type[feature_name] == "real":
            split = self.woe_dict[feature_name].split.copy()
            woe = self.woe_dict[feature_name].cod_dict
            split = enumerate(np.hstack([split, [np.inf]]))
            split = OrderedDict(split)

            spec_val = set(woe.keys()) - set(split.keys())
            spec_val = OrderedDict((key, woe[key]) for key in spec_val)

            split = OrderedDict((split[key], value) for (key, value) in woe.items() if key in split)
            split, spec_val = list(split.items()), list(spec_val.items())

            borders, values = list(zip(*split))
            new_borders = list(zip([-np.inf] + list(borders[:-1]), borders))
            new_borders = [('{:.2f}'.format(x[0]), '{:.2f}'.format(x[1])) for x in new_borders]

            split = list(zip(new_borders, values)) + spec_val

        elif self.private_features_type[feature_name] == "cat":
            split = list(self.woe_dict[feature_name].cod_dict.items())
        else:
            raise ValueError(f"Feature type {self.private_features_type[feature_name]} is not supported")

        split = [(x[1], str(x[0])) for x in split]
        return pd.Series(*(zip(*split)))

    def _infer_params(self, train: pd.DataFrame,
                      target_name: str,
                      features_type: Optional[Dict[str, str]] = None,
                      group_kf: Hashable = None,
                      max_bin_count: Optional[Dict[str, int]] = None,
                      features_monotone_constraints: Optional[Dict[str, str]] = None):
        """

        Args:
            train:
            target_name:
            features_type:
            group_kf:
            max_bin_count:
            features_monotone_constraints:

        Returns:

        """
        self.params = deepcopy(self._params)

        for k in ['th_const', 'th_nan', 'th_cat', 'min_bin_size']:
            val = self.params[k]
            self.params[k] = int(val * train.shape[0]) if 0 <= val < 1 else int(val)

        min_data_in_bin = [self.params['min_bin_size'], ]
        for m in self.params['min_bin_mults']:
            min_data_in_bin.append(int(m * self.params['min_bin_size']))

        self._tree_dict_opt = OrderedDict({"min_data_in_leaf": (self.params['min_bin_size'],),
                                           "min_data_in_bin": min_data_in_bin,
                                           "min_gain_to_split": self.params['min_gains_to_split']})

        # составим features_type
        self._features_type = features_type
        if self._features_type is None:
            self._features_type = {}

        assert target_name not in self._features_type, "target_name in features_type!!!"
        assert group_kf not in self._features_type, "group_kf in features_type!!!"

        droplist = [target_name]
        if group_kf is not None:
            droplist.append(group_kf)

        for col in train.columns.drop(droplist):
            if col not in self._features_type:
                self._features_type[col] = None

        # поработаем с монотонными ограничениями
        self.features_monotone_constraints = features_monotone_constraints
        if self.features_monotone_constraints is None:
            self.features_monotone_constraints = {}

        checklist = ['auto']
        if self.params['monotonic']:
            checklist.extend(['0', 0, None])

        for col in self._features_type:
            val = self.features_monotone_constraints.get(col)

            if val in checklist:
                new_val = get_monotonic_constr(col, train, target_name)
            elif val in ['0', 0, None]:
                new_val = '0'
            else:
                new_val = val

            self.features_monotone_constraints[col] = new_val

        # max_bin_count
        self.max_bin_count = max_bin_count
        if self.max_bin_count is None:
            self.max_bin_count = {}

        for col in self._features_type:
            if col not in self.max_bin_count:
                self.max_bin_count[col] = self.params['max_bin_count']

    def fit(self, train: pd.DataFrame,
            target_name: str,
            features_type: Optional[Dict[str, str]] = None,
            group_kf: Hashable = None,
            max_bin_count: Optional[Dict[str, int]] = None,
            features_monotone_constraints: Optional[Dict[str, str]] = None,
            validation: Optional[pd.DataFrame] = None):
        """

        Args:
        train: pandas.DataFrame
            Обучающая выборка
        target_name: str
            Имя колонки с целевой переменной
        features_type: dict
            Словарь с типами признаков,
            "cat" - категориальный, "real" - вещественный, "date" - для даты
        group_kf:
           Имя колнки для GroupKFold
        max_bin_count: dict
            Имя признака -> максимальное числов бинов
        features_monotone_constraints: dict
            Словарь с ограничениями на монотонность
            "-1" - признак монотонно убывает при возрастании целевой переменной
            "0" - нет ограничения на завсисимость. Переключается на auto в случае monotonic=True
            "1" - признак монотонно возрастает при возрастании целевой переменной
            "auto" - хочу монотонно, но не знаю как
            Для категориальных признаков указывать ничего не надо.
        validation: pandas.DataFrame
            Дополнительная валидационная выборка, используемая для выбора модели
            На текущий момент поддерживается:
            - отбор признаков по p-value

        Returns:

        """
        self._infer_params(train, target_name, features_type, group_kf, max_bin_count, features_monotone_constraints)

        if group_kf:
            group_kf = train[group_kf].values
        types_handler = TypesHandler(train=train,
                                     public_features_type=self._features_type,
                                     max_bin_count=self.max_bin_count,
                                     features_monotone_constraints=self.features_monotone_constraints)
        train_, self._public_features_type, self._private_features_type, max_bin_count, features_monotone_constraints \
            = types_handler.transform()
        del types_handler

        train_ = train_[[*self.private_features_type.keys(), target_name]]
        self.target = train_[target_name]
        self.feature_history = {key: None for key in self.private_features_type.keys()}
        # Отбрасывание колонок с нанами
        features_before = set(self._private_features_type.keys())
        train_, self._private_features_type = nan_constant_selector(train_, self.private_features_type,
                                                                    th_const=self.params['th_const'])
        features_after = set(self._private_features_type.keys())
        features_diff = features_before - features_after
        for feature in features_diff:
            self.feature_history[feature] = 'NaN values'
        # Первичный отсев по важности
        features_before = features_after
        train_, self._private_features_type = feature_imp_selector(train_, self.private_features_type, target_name,
                                                                   imp_th=self.params['imp_th'], imp_type=self.params['imp_type'],
                                                                   select_type=self.params['select_type'],
                                                                   process_num=self.params['n_jobs'])
        features_after = set(self._private_features_type.keys())
        features_diff = features_before - features_after
        for feature in features_diff:
            self.feature_history[feature] = 'Low importance'

        self._small_nans = SmallNans(th_nan=self.params['th_nan'], th_cat=self.params['th_cat'],
                                     cat_merge_to=self.params['cat_merge_to'],
                                     nan_merge_to=self.params['nan_merge_to'])  # класс для обработки нанов

        train_, spec_values = self._small_nans.fit_transform(train=train_, features_type=self.private_features_type)

        self._cv_split = cv_split_f(train_, self.target, group_kf, n_splits=self.params['n_folds'])

        params_gen = ((x,
                       deepcopy(train_[[x, target_name]]),
                       features_monotone_constraints[x],
                       max_bin_count[x], self.params['cat_alpha']) for x in self.private_features_type.keys())

        if self.params['n_jobs'] > 1:
            with Pool(self.params['n_jobs']) as pool:
                result = pool.starmap(self.feature_woe_transform, params_gen)
        else:
            result = []
            for params in params_gen:
                result.append(self.feature_woe_transform(*params))

        split_dict = dict(zip(self.private_features_type.keys(), result))
        split_dict = {key: split_dict[key] for key in split_dict if split_dict[key] is not None}

        features_before = features_after
        self._private_features_type = {x: self.private_features_type[x] for x in split_dict if
                                       x in split_dict.keys()}
        features_after = set(self._private_features_type.keys())
        features_diff = features_before - features_after
        for feature in features_diff:
            self.feature_history[feature] = 'Unable to WOE transform'

        # print(f"{split_dict.keys()} to selector !!!!!")
        logger.info(f"{split_dict.keys()} to selector !!!!!")
        self.split_dict = split_dict  # набор пар признаки - границы бинов
        self.train_df = self._train_encoding(train_, spec_values, self.params['oof_woe'])

        logger.info("Feature selection...")
        selector = Selector(interpreted_model=self.params['interpreted_model'],
                            train=self.train_df,
                            target=self.target,
                            features_type=self.private_features_type,
                            n_jobs=self.params['n_jobs'],
                            cv_split=self._cv_split
                            )

        best_features, self._sel_result = selector(pearson_th=self.params['pearson_th'],
                                                   auc_th=self.params['auc_th'],
                                                   vif_th=self.params['vif_th'],
                                                   l1_grid_size=self.params['l1_grid_size'],
                                                   l1_exp_scale=self.params['l1_exp_scale'],
                                                   auc_tol=self.params['auc_tol'],
                                                   feature_history=self.feature_history)

        # create validation data if it's defined and usefull
        valid_enc, valid_target = None, None
        if validation is not None and not self.params['regularized_refit']:
            valid_enc = self.test_encoding(validation, best_features)
            valid_target = validation[target_name]

        fit_result = self._clf_fit(self.train_df, best_features, self.feature_history, valid_enc, valid_target)

        self.features_fit = fit_result['features_fit']
        self._weights = fit_result['weights']
        self._intercept = fit_result['intercept']
        if 'b_var' in fit_result:
            self._b_var = fit_result['b_var']
        if 'p_vals' in fit_result:
            self._p_vals = fit_result['p_vals']

        if not self.params['debug']:
            del self.train_df
            del self.target

    def feature_woe_transform(self, feature_name: str, train_f: pd.DataFrame,
                              features_monotone_constraints: str, max_bin_count: int,
                              cat_alpha: float = 1.) -> SplitType:
        """
        Метод для кодирования признаков поодиночке

        Args:
            feature_name:
            train_f: обучающая выборка
            features_monotone_constraints: характер монотонности разюиения
            max_bin_count: максимальное число бинов в биннинге
            cat_alpha: регуляризация для категорий

        Returns:
            None, list
        """
        train_f = train_f.reset_index(drop=True)
        logger.info(f"{feature_name} processing...")
        target_name = train_f.columns[1]
        # Откидываем здесь закодированные маленькие категории/наны. Их не учитываем при определения бинов
        if np.issubdtype(train_f.dtypes[feature_name], np.number):
            nan_index = []
        else:
            sn_set = _small_nan_set if self.private_features_type[feature_name] == "cat" else _nan_set
            nan_index = train_f[feature_name].isin(sn_set)
            nan_index = np.where(nan_index.values)[0]

        cat_enc = None
        if self.private_features_type[feature_name] == "cat":
            cat_enc = CatEncoding(data=train_f)
            train_f = cat_enc(self._cv_split, nan_index, cat_alpha)

        train_f = train_f.iloc[np.setdiff1d(np.arange(train_f.shape[0]), nan_index), :]

        train_f = train_f.astype({feature_name: float, target_name: int})
        # нужный тип для lgb после нанов и маленьких категорий
        if train_f.shape[0] == 0:  # случай, если кроме нанов и маленьких категорий ничего не осталось
            split = [-np.inf]
            if self.private_features_type[feature_name] == "cat":
                return cat_enc.mean_target_reverse(split)
            elif self.private_features_type[feature_name] == "real":
                return split
            else:
                raise ValueError("self.features_type[feature] is cat or real")

        # подбор оптимальных параметров дерева
        tree_dict_opt = deepcopy(self._tree_dict_opt)
        if max_bin_count:  # ограничение на число бинов

            leaves_range = tuple(range(2, max_bin_count + 1))
            tree_dict_opt = OrderedDict({**self._tree_dict_opt,
                                         **{"num_leaves": leaves_range,
                                            "bin_construct_sample_cnt": (int(1e8),)},
                                         })

            # Еще фича force_single_split ..
            if self.params['force_single_split']:
                min_size = train_f.shape[0] - train_f[feature_name].value_counts(dropna=False).values[0]
                if self.params['th_const'] < min_size < self.params['min_bin_size']:
                    tree_dict_opt["min_data_in_leaf"] = [min_size, ]
                    tree_dict_opt["min_data_in_bin"] = [3, ]
                    tree_dict_opt["num_leaves"] = [2, ]

        tree_opt = TreeParamOptimizer(data=train_f,
                                      n_folds=self.params['n_folds'],
                                      params_range=collections.OrderedDict(**tree_dict_opt,
                                                                           **{"monotone_constraints": (
                                                                               features_monotone_constraints,)}))
        tree_param = tree_opt(3)
        # значение monotone_constraints содержится в tree_params
        # подбор подходяшего сплита на бины
        htransform = HTransform(train_f[feature_name],
                                train_f[target_name])
        split = htransform(tree_param)

        #  Обратная операция к mean_target_encoding
        if self.private_features_type[feature_name] == "cat":
            return cat_enc.mean_target_reverse(split)
        elif self.private_features_type[feature_name] == "real":
            return split
        else:
            raise ValueError("self.features_type[feature] is cat or real")

    def _train_encoding(self, train: pd.DataFrame,
                        spec_values: Dict,  # TODO: ref
                        folds_codding: bool) -> pd.DataFrame:
        """
        Кодирование train датасета

        Args:
        train: pandas.DataFrame
            DataFrame для преобразований
        spec_values: dict
            словарь для работы с нанами
        folds_codding: bool
           Флаг WoE кодирования по фолдам

        Returns:

        """
        woe_dict = dict()
        woe_list = []
        for feature in self.private_features_type:
            woe = WoE(f_type=self.private_features_type[feature], split=self.split_dict[feature],
                      woe_diff_th=self.params['woe_diff_th'])
            if folds_codding:
                df_cod = woe.fit_transform_cv(train[feature], self.target, spec_values=spec_values[feature],
                                              cv_index_split=self._cv_split)
                woe.fit(train[feature], self.target, spec_values=spec_values[feature])
            else:
                df_cod = woe.fit_transform(train[feature], self.target, spec_values=spec_values[feature])
            woe_dict[feature] = woe
            woe_list.append(df_cod)
        self.woe_dict = woe_dict
        train_tr = pd.concat(woe_list, axis=1)
        train_tr.columns = self.private_features_type.keys()
        return train_tr

    def _clf_fit(self, data_enc, features, feature_history=None, valid_enc=None, valid_target=None) -> dict:
        """
        Финальное переобучение модели

        Args:
            data_enc:
            features:
            feature_history:
            valid_enc:
            valid_target:

        Returns:

        """
        x_train, y_train = data_enc[features].values, self.target.values
        x_val, y_val = None, None
        p_vals = None

        result = dict()
        if self.params['regularized_refit']:
            w, i, neg = refit_reg(x_train, y_train,
                                  l1_grid_size=self.params['l1_grid_size'],
                                  l1_exp_scale=self.params['l1_exp_scale'],
                                  max_penalty=self._sel_result.reg_alpha,
                                  interp=self.params['interpreted_model'])
        else:
            if valid_enc is not None:
                x_val, y_val = valid_enc[features].values, valid_target.values

            w, i, neg, p_vals, b_var = refit_simple(x_train, y_train, interp=self.params['interpreted_model'],
                                                    p_val=self.params['p_val'], x_val=x_val, y_val=y_val)

            result['b_var'] = b_var

        _feats = np.array(features)[neg]

        features_before = set(features)
        features_fit = pd.Series(w, _feats)
        result['features_fit'] = features_fit
        features_after = set(features_fit.index)
        features_diff = features_before - features_after
        if feature_history is not None:
            for feature in features_diff:
                feature_history[feature] = 'Pruned during regression refit'

        if not self.params['regularized_refit']:
            result['p_vals'] = pd.Series(p_vals, list(_feats) + ['Intercept_'])

        logger.info(features_fit)
        result['weights'] = w
        result['intercept'] = i
        return result

    def test_encoding(self, test: pd.DataFrame, feats: Optional[List[str]] = None) -> pd.DataFrame:
        """
        Подготовка тестового датасета для обучения

        Args:
        test: pandas.DataFrame
            Тестовый датасет
        feats: list or None
            features names

        Returns:

        """
        if feats is None:
            feats = list(self.features_fit.index)

        feats_to_get = deepcopy(feats)

        for feat in feats:
            parts = feat.split('__F__')
            if len(parts) > 1:
                feats_to_get.append('__F__'.join(parts[:-1]))
        feats_to_get = [x for x in list(set(feats_to_get)) if x in test.columns]

        types = {}
        for feat in feats_to_get:
            if feat in self._public_features_type:
                types[feat] = self._public_features_type[feat]

        types_handler = TypesHandler(train=test[feats_to_get], public_features_type=types)
        test_, _, _, _, _ = types_handler.transform()
        del types_handler

        woe_list = []
        test_, spec_values = self._small_nans.transform(test_, feats)
        # здесь дебажный принт
        logger.debug(spec_values)
        for feature in feats:
            df_cod = self.woe_dict[feature].transform(test_[feature], spec_values[feature])
            woe_list.append(df_cod)

        test_tr = pd.concat(woe_list, axis=1)
        test_tr.columns = feats
        return test_tr[feats]

    def predict_proba(self, test: pd.DataFrame) -> np.ndarray:
        """
        Сделать предсказание на тестовый датасет

        Args:
        test: pd.DataFrame
            Тестовый датасет предобработанный для предсказания

        Returns:
            np.ndarray
        """
        test_tr = self.test_encoding(test)
        prob = 1 / (1 + np.exp(-(np.dot(test_tr.values, self.weights) + self.intercept)))
        return prob

    def get_model_represenation(self):
        """
        Получить скоркарту

        Returns:

        """
        features = list(self.features_fit.index)
        result = dict()
        for feature in features:
            feature_data = dict()
            woe = self.woe_dict[feature]
            feature_data['f_type'] = woe.f_type

            if woe.f_type == 'real':
                feature_data['splits'] = [0 + round(float(x), 6) for x in woe.split]
            else:
                feature_data['cat_map'] = {str(k): int(v) for k, v in woe.split.items()}
                spec_vals = self._small_nans.cat_encoding[feature]
                feature_data['spec_cat'] = (spec_vals[0], spec_vals[2])

            feature_data['cod_dict'] = {int(k): (0 + round(float(v), 6))
                                        for k, v in woe.cod_dict.items()
                                        if type(k) is int or type(k) is float}

            feature_data['weight'] = float(self.features_fit[feature])
            feature_data['nan_value'] = self._small_nans.all_encoding[feature]
            feature_data['spec_cod'] = {k: (0 + round(float(v), 6))
                                        for k, v in woe.cod_dict.items()
                                        if type(k) is str}

            result[feature] = feature_data

        return {'features': result, 'intercept': float(self.intercept)}

    def get_sql_inference_query(self, table_name: str) -> str:
        """
        Сгенерировать SQL-запрос для прогноза по данным, содержащимся в таблице в БД

        Args:
        table_name: str
            Имя таблицы в БД, содержащей данные, на которых требуется сделать прогноз

        Returns:
            query_string: str
                SQL-запрос для предсказания результатов по таблице в БД
        """
        model_data = self.get_model_represenation()
        return get_sql_query(model_data, table_name)
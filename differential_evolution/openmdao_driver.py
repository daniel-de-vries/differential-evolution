from six import iteritems, itervalues, next

import numpy as np

import openmdao
from openmdao.core.driver import Driver, RecordingDebugging
from openmdao.utils.mpi import MPI
from openmdao.core.analysis_error import AnalysisError

try:
    from tqdm import tqdm
except ModuleNotFoundError:
    tqdm = lambda i, totals: i

from . import DifferentialEvolution, EvolutionStrategy

if not MPI:
    rank = 0
else:
    rank = MPI.COMM_WORLD.rank


class DifferentialEvolutionDriver(Driver):

    def __init__(self, **kwargs):
        """
        Initialize the DifferentialEvolution driver.

        Parameters
        ----------
        **kwargs : dict of keyword arguments
            Keyword arguments that will be mapped into the Driver options.
        """
        super().__init__(**kwargs)

        # What we support
        self.supports['integer_design_vars'] = True
        self.supports['inequality_constraints'] = True
        self.supports['equality_constraints'] = True
        self.supports['multiple_objectives'] = True

        # What we don't support yet
        self.supports['two_sided_constraints'] = False
        self.supports['linear_constraints'] = False
        self.supports['simultaneous_derivatives'] = False
        self.supports['active_set'] = False

        self._desvar_idx = {}
        self._es = None
        self._de = None

        # Support for Parallel models.
        self._concurrent_pop_size = 0
        self._concurrent_color = 0

    def _declare_options(self):
        """
        Declare options before kwargs are processed in the init method.
        """
        self.options.declare("strategy", default="rand-to-best/1/exp/random",
                             desc="Evolution strategy to use for the differential evolution.")
        self.options.declare('Pm',
                             desc='Mutation rate.', default=0.85, lower=0., upper=1.)
        self.options.declare('Pc', default=1., lower=0., upper=1.,
                             desc='Crossover rate.')
        self.options.declare('max_gen', default=100,
                             desc='Number of generations before termination.')
        self.options.declare("tolx", default=1e-8,
                             desc="Tolerance of the design vectors' spread.")
        self.options.declare("tolf", default=1e-8,
                             desc="Tolerance of the fitness spread.")
        self.options.declare('pop_size', default=0,
                             desc='Number of points in the GA. Set to 0 and it will be computed '
                                  'as four times the number of bits.')
        self.options.declare('run_parallel', types=bool, default=False,
                             desc='Set to True to execute the points in a generation in parallel.')
        self.options.declare('procs_per_model', default=1, lower=1,
                             desc='Number of processors to give each model under MPI.')
        self.options.declare('penalty_parameter', default=10., lower=0.,
                             desc='Penalty function parameter.')
        self.options.declare('penalty_exponent', default=1.,
                             desc='Penalty function exponent.')
        self.options.declare('multi_obj_weights', default={}, types=(dict),
                             desc='Weights of objectives for multi-objective optimization.'
                                  'Weights are specified as a dictionary with the absolute names'
                                  'of the objectives. The same weights for all objectives are assumed, '
                                  'if not given.')
        self.options.declare('multi_obj_exponent', default=1., lower=0.,
                             desc='Multi-objective weighting exponent.')
        self.options.declare("show_progress", default=False,
                             desc="Set to true if a progress bar should be shown.")

    def _setup_driver(self, problem):
        """
        Prepare the driver for execution.

        This is the final thing to run during setup.

        Parameters
        ----------
        problem : <Problem>
            Pointer to the containing problem.
        """
        super()._setup_driver(problem)

        model_mpi = None
        comm = self._problem.comm
        if self._concurrent_pop_size > 0:
            model_mpi = (self._concurrent_pop_size, self._concurrent_color)
        elif not self.options['run_parallel']:
            comm = None

        self._es = EvolutionStrategy(self.options["strategy"])
        self._de = DifferentialEvolution(strategy=self._es, mut=self.options["Pm"], crossp=self.options["Pc"],
                                         max_gen=self.options["max_gen"],
                                         tolx=self.options["tolx"], tolf=self.options["tolf"],
                                         n_pop=self.options["pop_size"], seed=None,
                                         comm=comm, model_mpi=model_mpi)

    def _setup_comm(self, comm):
        """
        Perform any driver-specific setup of communicators for the model.

        Here, we generate the model communicators.

        Parameters
        ----------
        comm : MPI.Comm or <FakeComm> or None
            The communicator for the Problem.

        Returns
        -------
        MPI.Comm or <FakeComm> or None
            The communicator for the Problem model.
        """
        procs_per_model = self.options['procs_per_model']
        if MPI and self.options['run_parallel'] and procs_per_model > 1:

            full_size = comm.size
            size = full_size // procs_per_model
            if full_size != size * procs_per_model:
                raise RuntimeError("The total number of processors is not evenly divisible by the "
                                   "specified number of processors per model.\n Provide a "
                                   "number of processors that is a multiple of %d, or "
                                   "specify a number of processors per model that divides "
                                   "into %d." % (procs_per_model, full_size))
            color = comm.rank % size
            model_comm = comm.Split(color)

            # Everything we need to figure out which case to run.
            self._concurrent_pop_size = size
            self._concurrent_color = color

            return model_comm

        self._concurrent_pop_size = 0
        self._concurrent_color = 0
        return comm

    def _get_name(self):
        """
        Get name of current Driver.

        Returns
        -------
        str
            Name of current Driver.
        """
        return "DifferentialEvolution"

    def run(self):
        """
        Execute the differential evolution algorithm.

        Returns
        -------
        boolean
            Failure flag; True if failed to converge, False is successful.
        """
        model = self._problem.model
        de = self._de

        de.strategy = EvolutionStrategy(self.options['strategy'])
        de.f = self.options['Pm']
        de.cr = self.options['Pc']
        de.n_pop = self.options['pop_size']
        de.max_gen = self.options["max_gen"]
        de.tolx = self.options["tolx"]
        de.tolf = self.options['tolf']

        self._check_for_missing_objective()

        # Size design variables.
        desvars = self._designvars
        desvar_vals = self.get_design_var_values()

        count = 0
        for name, meta in iteritems(desvars):
            if name in self._designvars_discrete:
                val = desvar_vals[name]
                if np.isscalar(val):
                    size = 1
                else:
                    size = len(val)
            else:
                size = meta['size']
            self._desvar_idx[name] = (count, count + size)
            count += size

        bounds = []
        x0 = np.empty(count)

        # Figure out bounds vectors and initial design vars
        for name, meta in iteritems(desvars):
            i, j = self._desvar_idx[name]
            lb = meta['lower']
            if isinstance(lb, float):
                lb = [lb] * (j - i)
            ub = meta['upper']
            if isinstance(ub, float):
                ub = [ub] * (j - i)
            for k in range(j - i):
                bounds += [(lb[k], ub[k])]
            x0[i:j] = desvar_vals[name]

        de.init(self.objective_callback, bounds)

        gen_iter = de
        if rank == 0 and self.options["show_progress"] and tqdm is not None:
            gen_iter = tqdm(gen_iter, total=self.options["max_gen"])

        last_generation = None
        for generation in gen_iter:
            if rank == 0:
                s = " "
                if tqdm is None:
                    s += f"gen: {generation.generation:>5g} / {generation.max_gen}, "
                s += f"f*: {generation.best_fit:> 10.4g}, " \
                     f"dx: {generation.dx:> 10.4g} " \
                     f"df: {generation.df:> 10.4g}".replace('\n', '')
                print(s.replace('\n', ''))
            last_generation = generation

        best = last_generation.best if last_generation is not None else de.best
        # Pull optimal parameters back into framework and re-run, so that
        # framework is left in the right final state
        for name in desvars:
            i, j = self._desvar_idx[name]
            val = best[i:j]
            self.set_design_var(name, val)

        with RecordingDebugging(self._get_name(), self.iter_count, self) as rec:
            model.run_solve_nonlinear()
            rec.abs = 0.0
            rec.rel = 0.0
        self.iter_count += 1

        return False

    def objective_callback(self, x):
        r"""
        Evaluate problem objective at the requested point.

        In case of multi-objective optimization, a simple weighted sum method is used:

        .. math::

           f = (\sum_{k=1}^{N_f} w_k \cdot f_k)^a

        where :math:`N_f` is the number of objectives and :math:`a>0` is an exponential
        weight. Choosing :math:`a=1` is equivalent to the conventional weighted sum method.

        The weights given in the options are normalized, so:

        .. math::

            \sum_{k=1}^{N_f} w_k = 1

        If one of the objectives :math:`f_k` is not a scalar, its elements will have the same
        weights, and it will be normed with length of the vector.

        Takes into account constraints with a penalty function.

        All constraints are converted to the form of :math:`g_i(x) \leq 0` for
        inequality constraints and :math:`h_i(x) = 0` for equality constraints.
        The constraint vector for inequality constraints is the following:

        .. math::

           g = [g_1, g_2  \dots g_N], g_i \in R^{N_{g_i}}

           h = [h_1, h_2  \dots h_N], h_i \in R^{N_{h_i}}

        The number of all constraints:

        .. math::

           N_g = \sum_{i=1}^N N_{g_i},  N_h = \sum_{i=1}^N N_{h_i}

        The fitness function is constructed with the penalty parameter :math:`p`
        and the exponent :math:`\kappa`:

        .. math::

           \Phi(x) = f(x) + p \cdot \sum_{k=1}^{N^g}(\delta_k \cdot g_k)^{\kappa}
           + p \cdot \sum_{k=1}^{N^h}|h_k|^{\kappa}

        where :math:`\delta_k = 0` if :math:`g_k` is satisfied, 1 otherwise

        .. note::

            The values of :math:`\kappa` and :math:`p` can be defined as driver options.

        Parameters
        ----------
        x : ndarray
            Value of design variables.
        icase : int
            Case number, used for identification when run in parallel.

        Returns
        -------
        float
            Objective value
        bool
            Success flag, True if successful
        int
            Case number, used for identification when run in parallel.
        """
        model = self._problem.model
        success = 1

        objs = self.get_objective_values()
        nr_objectives = len(objs)

        # Single objective, if there is nly one objective, which has only one element
        is_single_objective = (nr_objectives == 1) and (len(next(itervalues(objs))) == 1)

        obj_exponent = self.options['multi_obj_exponent']
        if self.options['multi_obj_weights']:  # not empty
            obj_weights = self.options['multi_obj_weights']
        else:
            # Same weight for all objectives, if not specified
            obj_weights = {name: 1. for name in objs.keys()}
        sum_weights = sum(itervalues(obj_weights))

        for name in self._designvars:
            i, j = self._desvar_idx[name]
            self.set_design_var(name, x[i:j])

        # a very large number, but smaller than the result of nan_to_num in Numpy
        almost_inf = openmdao.INF_BOUND

        # Execute the model
        with RecordingDebugging(self._get_name(), self.iter_count, self) as rec:
            self.iter_count += 1
            try:
                model.run_solve_nonlinear()

            # Tell the optimizer that this is a bad point.
            except AnalysisError:
                model._clear_iprint()
                success = 0

            obj_values = self.get_objective_values()
            if is_single_objective:  # Single objective optimization
                obj = next(itervalues(obj_values))  # First and only key in the dict
            else:  # Multi-objective optimization with weighted sums
                weighted_objectives = np.array([])
                for name, val in iteritems(obj_values):
                    # element-wise multiplication with scalar
                    # takes the average, if an objective is a vector
                    try:
                        weighted_obj = val * obj_weights[name] / val.size
                    except KeyError:
                        msg = ('Name "{}" in "multi_obj_weights" option '
                               'is not an absolute name of an objective.')
                        raise KeyError(msg.format(name))
                    weighted_objectives = np.hstack((weighted_objectives, weighted_obj))

                obj = sum(weighted_objectives / sum_weights)**obj_exponent

            # Parameters of the penalty method
            penalty = self.options['penalty_parameter']
            exponent = self.options['penalty_exponent']

            if penalty == 0:
                fun = obj
            else:
                constraint_violations = np.array([])
                for name, val in iteritems(self.get_constraint_values()):
                    con = self._cons[name]
                    # The not used fields will either None or a very large number
                    if (con['lower'] is not None) and (con['lower'] > -almost_inf):
                        diff = val - con['lower']
                        violation = np.array([0. if d >= 0 else abs(d) for d in diff])
                    elif (con['upper'] is not None) and (con['upper'] < almost_inf):
                        diff = val - con['upper']
                        violation = np.array([0. if d <= 0 else abs(d) for d in diff])
                    elif (con['equals'] is not None) and (abs(con['equals']) < almost_inf):
                        diff = val - con['equals']
                        violation = np.absolute(diff)
                    constraint_violations = np.hstack((constraint_violations, violation))
                fun = obj + penalty * sum(np.power(constraint_violations, exponent))
            # Record after getting obj to assure they have
            # been gathered in MPI.
            rec.abs = 0.0
            rec.rel = 0.0

        # print("Functions calculated")
        # print(x)
        # print(obj)
        return fun[0]

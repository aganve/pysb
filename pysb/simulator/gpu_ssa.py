from __future__ import print_function

#
try:
    import pycuda
    import pycuda.autoinit
    import pycuda as cuda
    import pycuda.compiler
    import pycuda.tools as tools
    import pycuda.driver as driver
    import pycuda.gpuarray as gpuarray
except ImportError:
    pycuda = None

import re
import numpy as np
import os
import sympy
import time
from pysb.logging import setup_logger
import logging
from pysb.pathfinder import get_path
from pysb.core import Expression
from pysb.bng import generate_equations
from pysb.simulator.base import Simulator, SimulationResult, SimulatorException


class GPUSimulator(Simulator):
    """
    GPU simulator

    Parameters
    ----------
    model : pysb.Model
        Model to simulate.
    tspan : vector-like, optional
        Time values over which to simulate. The first and last values define
        the time range. Returned trajectories are sampled at every value unless
        the simulation is interrupted for some reason, e.g., due to
        satisfaction
        of a logical stopping criterion (see 'tout' below).
    initials : vector-like or dict, optional
        Values to use for the initial condition of all species. Ordering is
        determined by the order of model.species. If not specified, initial
        conditions will be taken from model.initial_conditions (with
        initial condition parameter values taken from `param_values` if
        specified).
    param_values : vector-like or dict, optional
        Values to use for every parameter in the model. Ordering is
        determined by the order of model.parameters.
        If passed as a dictionary, keys must be parameter names.
        If not specified, parameter values will be taken directly from
        model.parameters.
    verbose : bool, optional (default: False)
        Verbose output.

    Attributes
    ----------
    verbose: bool
        Verbosity flag passed to the constructor.
    model : pysb.Model
        Model passed to the constructor.
    tspan : vector-like
        Time values passed to the constructor.
    """
    _supports = {'multi_initials': True, 'multi_param_values': True}

    def __init__(self, model, verbose=False, tspan=None, precision=np.float32,
                 **kwargs):
        super(GPUSimulator, self).__init__(model, verbose, **kwargs)

        if pycuda is None:
            raise SimulatorException('pycuda library was not found and is '
                                     'required for {}'.format(
                self.__class__.__name__))

        generate_equations(self._model)
        self._precision = precision
        self.tout = None
        self.tspan = tspan
        self.verbose = verbose

        # private attribute
        self._threads = 32
        self._parameter_number = len(self._model.parameters)
        self._n_species = len(self._model.species)
        self._n_reactions = len(self._model.reactions)
        self._step_0 = True
        self._code = self._pysb_to_cuda()
        self._ssa_all = None
        self._kernel = None
        self._param_tex = None
        self._ssa = None
        self._ssa_all = None

        if verbose:
            setup_logger(logging.INFO)
        self._logger.info("Initialized GPU class")

    def _pysb_to_cuda(self):
        """ converts pysb reactions to cuda compilable code

        """
        p = re.compile('\s')
        stoich_matrix = (_rhs(self._model) + _lhs(self._model)).T

        all_reactions = []
        for rxn_number, rxn in enumerate(stoich_matrix.T):
            changes = []
            for index, change in enumerate(rxn):
                if change != 0:
                    changes.append([index, change])
            all_reactions.append(changes)

        params_names = [g.name for g in self._model.parameters]
        _reaction_number = len(self._model.reactions)

        stoich_string = ''
        l_lim = self._n_species - 1
        r_lim = self._n_reactions - 1
        for i in range(0, self._n_reactions):
            for j in range(0, len(stoich_matrix)):
                stoich_string += "%s" % repr(stoich_matrix[j][i])
                if not (i == l_lim and j == r_lim):
                    stoich_string += ','
            stoich_string += '\n'
        hazards_string = ''
        pattern = "(__s\d+)\*\*(\d+)"
        for n, rxn in enumerate(self._model.reactions):

            hazards_string += "\th[%s] = " % repr(n)
            rate = sympy.fcode(rxn["rate"])
            rate = re.sub('d0', '', rate)
            rate = p.sub('', rate)
            expr_strings = {
                e.name: '(%s)' % sympy.ccode(
                    e.expand_expr(expand_observables=True)
                ) for e in self.model.expressions}
            # expand only expressions used in the rate eqn
            for e in {sym for sym in rxn["rate"].atoms()
                      if isinstance(sym, Expression)}:
                rate = re.sub(r'\b%s\b' % e.name,
                              expr_strings[e.name],
                              rate)

            matches = re.findall(pattern, rate)
            for m in matches:
                repl = m[0]
                for i in range(1, int(m[1])):
                    repl += "*(%s-%d)" % (m[0], i)
                rate = re.sub(pattern, repl, rate)

            rate = re.sub(r'_*s(\d+)',
                          lambda m: 'y[%s]' % (int(m.group(1))),
                          rate)
            for q, prm in enumerate(params_names):
                rate = re.sub(r'\b(%s)\b' % prm, 'param_arry[%s]' % q, rate)
            items = rate.split('*')
            rate = ''
            for i in items:
                if i.startswith('param_arry'):
                    rate += i + '*'
            for i in sorted(items):
                if i.startswith('param_arry'):
                    continue
                rate += i + '*'
            rate = re.sub('\*$', '', rate)
            rate = re.sub('d0', '', rate)
            rate = p.sub('', rate)
            rate = rate.replace('pow', 'powf')
            hazards_string += rate + ";\n"
        template_code = _load_template()
        cs_string = template_code.format(n_species=self._n_species,
                                         n_params=self._parameter_number,
                                         n_reactions=_reaction_number,
                                         hazards=hazards_string,
                                         stoch=stoich_string,
                                         )
        if self._precision == np.float64:
            cs_string = cs_string.replace('float', 'double')
            cs_string = cs_string.replace('cuda_uniform',
                                          'cuda_uniform_double')
        self._logger.debug("Converted PySB model to pycuda code")
        return cs_string

    def _compile(self, code):

        if self.verbose:
            self._logger.info("Output cuda file to ssa_cuda_code.cu")
            with open("ssa_cuda_code.cu", "w") as source_file:
                source_file.write(code)
        nvcc_bin = get_path('nvcc')
        self._logger.debug("Compiling CUDA code")
        self._kernel = pycuda.compiler.SourceModule(code, nvcc=nvcc_bin,
                                                    no_extern_c=True,
                                                    keep=True)

        self._ssa = self._kernel.get_function("Gillespie_one_step")
        self._ssa_all = self._kernel.get_function("Gillespie_all_steps")
        self._logger.debug("Compiled CUDA code")

    def _run(self, start_time, end_time, params, initials, threads, blocks):
        """

        Parameters
        ----------
        start_time : np.float
            initial time point
        end_time : np.float
            time point to finish
        params : list_like
            param_values for simulation
        initials : list_like
            initial conditions to pass to model
        threads : int
            Number of threads per block
        blocks : int
            Number of blocks per gpu
        Returns
        -------

        """

        if self._step_0:
            self._setup()

            if self.verbose:
                self._print_verbose(self._ssa)

        n_simulations = len(params)

        total_threads = blocks * threads

        param_array_gpu = gpuarray.to_gpu(
            self._create_gpu_array(params, total_threads, self._precision)
        )

        species_matrix_gpu = gpuarray.to_gpu(
            self._create_gpu_array(initials, total_threads, np.int32)
        )

        result = driver.managed_zeros(
            shape=(total_threads, self._n_species),
            dtype=np.int32, mem_flags=driver.mem_attach_flags.GLOBAL
        )

        # place starting time on GPU
        start_time_gpu = gpuarray.to_gpu(
            np.array(start_time, dtype=self._precision)
        )

        # allocate and upload time to GPU
        last_time_gpu = gpuarray.to_gpu(
            np.zeros(n_simulations, dtype=self._precision)
        )

        # run single step
        self._ssa(species_matrix_gpu, result, start_time_gpu, end_time,
                  last_time_gpu, param_array_gpu,
                  block=(threads, 1, 1),
                  grid=(blocks, 1))

        # Wait for kernel completion before host access
        pycuda.autoinit.context.synchronize()

        # retrieve and store results
        result = result[:n_simulations, :]

        current_time = last_time_gpu.get()[:n_simulations]

        return result, current_time

    def _run_all(self, timepoints, params, initials, threads, blocks):

        # compile kernel and send parameters to GPU
        if self._step_0:
            self._setup()

        if self.verbose:
            self._print_verbose(self._ssa_all)

        total_threads = int(blocks * threads)

        param_array_gpu = gpuarray.to_gpu(
            self._create_gpu_array(params, total_threads, self._precision)
        )

        species_matrix_gpu = gpuarray.to_gpu(
            self._create_gpu_array(initials, total_threads, np.int32)
        )

        # allocate and upload time to GPU
        time_points_gpu = gpuarray.to_gpu(
            np.array(timepoints, dtype=self._precision)
        )

        # allocate space on GPU for results
        result = driver.managed_zeros(
            shape=(total_threads, len(timepoints), self._n_species),
            dtype=np.int32, mem_flags=driver.mem_attach_flags.GLOBAL
        )

        # perform simulation
        self._ssa_all(species_matrix_gpu, result, time_points_gpu,
                      np.int32(len(timepoints)), param_array_gpu,
                      block=(threads, 1, 1), grid=(blocks, 1))

        # Wait for kernel completion before host access
        pycuda.autoinit.context.synchronize()

        # retrieve and store results, only keeping n_simulations
        # actual simulations we will return
        n_simulations = len(params)
        return result[:n_simulations, :, :]

    def run(self, tspan=None, param_values=None, initials=None, number_sim=0,
            threads=32):

        if param_values is None:
            # Run simulation using same param_values
            num_particles = int(number_sim)
            nominal_values = np.array(
                [p.value for p in self._model.parameters])
            param_values = np.zeros((num_particles, len(nominal_values)),
                                    dtype=self._precision)
            param_values[:, :] = nominal_values
        self.param_values = param_values

        if initials is None:
            # Run simulation using same initial conditions
            species_names = [str(s) for s in self._model.species]
            initials = np.zeros(len(species_names))
            for ic in self._model.initial_conditions:
                initials[species_names.index(str(ic[0]))] = int(ic[1].value)
            initials = np.repeat([initials], param_values.shape[0], axis=0)
            self.initials = initials

        if tspan is None:
            tspan = self.tspan

        tout = [tspan for _ in range(len(param_values))]
        t_out = np.array(tspan, dtype=self._precision)

        if threads is None:
            threads = self._threads

        size_params = param_values.shape[0]

        blocks = self.get_blocks(size_params, threads)

        self._logger.info("Starting {} simulations".format(number_sim))

        timer_start = time.time()
        result = self._run_all(t_out, param_values, initials, threads, blocks)
        timer_end = time.time()
        self._logger.info("{} simulations "
                          "in {}s".format(number_sim, timer_end - timer_start))

        return SimulationResult(self, tout, result)

    def run_one_step(self, tspan=None, param_values=None, initials=None,
                     number_sim=1, threads=32, verbose=False):

        if param_values is None:
            # Run simulation using same param_values
            num_particles = int(number_sim)
            nominal_values = np.array(
                [p.value for p in self._model.parameters])
            param_values = np.zeros((num_particles, len(nominal_values)),
                                    dtype=self._precision)
            param_values[:, :] = nominal_values
            self.param_values = param_values

        if initials is None:
            # Run simulation using same initial conditions
            species_names = [str(s) for s in self._model.species]
            initials = np.zeros(len(species_names))
            for ic in self._model.initial_conditions:
                initials[species_names.index(str(ic[0]))] = int(ic[1].value)
            initials = np.repeat([initials], param_values.shape[0], axis=0)
            self.initials = initials

        if tspan is None:
            tspan = self.tspan
        tspan = np.array(tspan, dtype=self._precision)
        tout = [tspan for _ in range(len(param_values))]

        t_out = np.array(tout, dtype=self._precision)
        len_time = len(tspan)

        if threads is None:
            threads = self._threads

        size_params = param_values.shape[0]

        blocks = self.get_blocks(size_params, threads)

        n_simulations = len(param_values)

        final_result = np.zeros((n_simulations, len_time, self._n_species),
                                dtype=np.int32)
        start_array = initials
        final_result[:, 0, :] = start_array
        start_time = t_out[:, 0]
        timer_start = time.time()

        # loops through each step
        for n, i in enumerate(tspan):
            if verbose:
                self._logger.info('{} out of {}'.format(n, len_time))
            if n == 0:
                continue

            end = i
            result, end_time = self._run(start_time, end, param_values,
                                         start_array, threads, blocks)
            t_out[:, n] = i
            start_time = end_time
            # print(i, start_time)
            final_result[:, n, :] = result
            start_array = result
        timer_end = time.time()
        self._logger.info("{} simulations "
                          "in {}s".format(number_sim, timer_end - timer_start))

        return SimulationResult(self, tout, final_result)

    def _print_verbose(self, code):
        self._logger.info("threads = {}".format(self._threads))

        self._logger.debug("Local memory  = {}".format(code.local_size_bytes))
        self._logger.debug("shared memory = {}".format(code.shared_size_bytes))
        self._logger.debug("registers  = {}".format(code.num_regs))

        occ = tools.OccupancyRecord(tools.DeviceData(),
                                    threads=self._threads,
                                    shared_mem=code.shared_size_bytes,
                                    registers=code.num_regs)
        self._logger.debug("tb_per_mp  = {}".format(occ.tb_per_mp))
        self._logger.debug("limited by = {}".format(occ.limited_by))
        self._logger.debug("occupancy  = {}".format(occ.occupancy))
        self._logger.debug(
            "tb_per_mp_limits  = {}".format(occ.tb_per_mp_limits))

    def _setup(self):
        self._compile(self._code)
        self._step_0 = False

    def _create_gpu_array(self, values, total_threads, prec):

        # Create species matrix on GPU
        # will make according to number of total threads, not n_simulations
        gpu_array = np.zeros((total_threads, values.shape[1]), dtype=prec)
        # Filling species matrix
        # Note that this might not fill entire array that was created.
        # The rest of the array will be zeros to fill up GPU.
        for i in range(len(values)):
            for j in range(values.shape[1]):
                gpu_array[i, j] = values[i, j]
        return gpu_array

    def _create_gpu_init(self, initials, total_threads):

        # Create species matrix on GPU
        # will make according to number of total threads, not n_simulations
        species_matrix = np.zeros((total_threads, self._n_species),
                                  dtype=np.int32)
        # Filling species matrix
        # Note that this might not fill entire array that was created.
        # The rest of the array will be zeros to fill up GPU.
        for i in range(len(initials)):
            for j in range(self._n_species):
                species_matrix[i][j] = initials[i][j]
        return species_matrix

    @staticmethod
    def get_blocks(n_params, threads):
        if n_params % threads == 0:
            return int(n_params / threads)
        else:
            return int(n_params / threads) + 1

    @staticmethod
    def get_gpu_settings(parameters):
        """
        Gathers optimal number of _threads per block given size of parameters
        :return _blocks, _threads
        """
        max_threads = tools.DeviceData().max_threads
        max_threads = 256
        warp_size = tools.DeviceData().warp_size
        max_warps = max_threads / warp_size
        threads = max_warps * warp_size
        if len(parameters) % threads == 0:
            blocks = len(parameters) / threads
        else:
            blocks = len(parameters) / threads + 1
        return blocks, threads


def _lhs(model):
    """
    Left hand side
    """
    left_side = np.zeros((len(model.reactions), len(model.species)),
                         dtype=np.int32)
    for i in range(len(model.reactions)):
        for j in range(len(model.species)):
            stoich = 0
            for k in model.reactions[i]['reactants']:
                if j == k:
                    stoich += 1
            left_side[i, j] = stoich
    return left_side * -1


def _rhs(model):
    """

    Right hand side of matrix

    """
    right_side = np.zeros((len(model.reactions), len(model.species)),
                          dtype=np.int32)
    for i in range(len(model.reactions)):
        for j in range(len(model.species)):
            stoich = 0
            for k in model.reactions[i]['products']:
                if j == k:
                    stoich += 1
            right_side[i, j] = stoich
    return right_side


def _load_template():
    with open(os.path.join(os.path.dirname(__file__),
                           'pycuda_templates',
                           'gillespie_template.cu'), 'r') as f:
        gillespie_code = f.read()
    return gillespie_code

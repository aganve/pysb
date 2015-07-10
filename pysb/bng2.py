import pysb.core
from pysb.generator.bng import BngGenerator
import os
import subprocess
import random
import re
import itertools
import sympy
import numpy
from StringIO import StringIO
import pysb.bng  

def run_ssa(model, tspan, param_values=None,initial_changes=None, output_dir=os.getcwd(), output_file_basename=None, cleanup=True, verbose=False, n_runs=1, **additional_args):
    """
    Simulate a model with BNG's SSA simulator and return the trajectories.

    Parameters
    ----------
    model : Model
        Model to simulate.
    tspan : vector-like
        Time values over which to integrate. The first and last values define
        the time range, and the returned trajectories will be sampled at every
        value.
    output_dir : string, optional
        Location for temporary files generated by BNG. Defaults to '/tmp'.
    output_file_basename : string, optional
        The basename for the .bngl, .gdat, .cdat, and .net files that are
        generated by BNG. If None (the default), creates a basename from the
        model name, process ID, and a random integer in the range (0, 100000).
    cleanup : bool, optional
        If True (default), delete the temporary files after the simulation is
        finished. If False, leave them in place (in `output_dir`). Useful for
        debugging.
    verbose: bool, optional
        If True, print BNG screen output.
    additional_args: kwargs, optional
        Additional arguments to pass to BioNetGen

    """
    ssa_args = "t_start=>%s,sample_times=>%s" % (str(tspan[0]),str(list(tspan)))
    for key,val in additional_args.items(): ssa_args += ", %s=>%s" % (key,"\""+str(val)+"\"" if isinstance(val,str) else str(val))
    if verbose: ssa_args += ", verbose=>1"
    run_ssa_code =  "begin actions\n"
    run_ssa_code += "\tgenerate_network({overwrite=>1})\n"
    if n_runs == 1: 
        run_ssa_code += "\tsimulate_pla({%s})\n" % (ssa_args)
    else:
        for n in range(n_runs):
            run_ssa_code += "\tsimulate({method=>\"ssa\",%s, %s })\n" % (ssa_args, "prefix=>\""+str(n)+"\"")
            run_ssa_code += "\tresetConcentrations()\n"
    run_ssa_code += "end actions\n"

    
    if param_values is not None:
        if len(param_values) != len(model.parameters):
            raise Exception("param_values must be the same length as model.parameters")
        for i in range(len(param_values)):
            model.parameters[i].value = param_values[i]
    #print model.initial_conditions
    #for each in model.parameters:
    #    print each
    if initial_changes is not None:
        original_values = {}
        for cp, value_obj in model.initial_conditions:
                    if value_obj.name in initial_changes:
                        original_values[value_obj.name] = value_obj.value
                        value_obj.value = initial_changes[value_obj.name]
    #print model.initial_conditions
    #for each in model.parameters:
    #    print each

    gen = BngGenerator(model)

    if output_file_basename is None:
        output_file_basename = '%s_temp' % (model.name)

    if os.path.exists(output_file_basename + '.bngl'):
        print "WARNING! File %s already exists!" % (output_file_basename + '.bngl')
        output_file_basename += '_1'

    bng_filename = output_file_basename + '.bngl'
    gdat_filename = output_file_basename + '.gdat'
    cdat_filename = output_file_basename + '.cdat'
    net_filename = output_file_basename + '.net'

    output = StringIO()


    working_dir = os.getcwd()
    os.chdir(output_dir)
    bng_file = open(bng_filename, 'w')
    bng_file.write(gen.get_content())
    bng_file.write(run_ssa_code)
    bng_file.close()
    p = subprocess.Popen(['perl', pysb.bng._get_bng_path(), bng_filename],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if verbose:
        for line in iter(p.stdout.readline, b''):
            print line,
    (p_out, p_err) = p.communicate()
    if p.returncode:
        raise GenerateNetworkError(p_out.rstrip("at line")+"\n"+p_err.rstrip())
    if initial_changes is not None:
        for cp, value_obj in model.initial_conditions:
            if value_obj.name in original_values:
                value_obj.value = original_values[value_obj.name]
    os.chdir("..")
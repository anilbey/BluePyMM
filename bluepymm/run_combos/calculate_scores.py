"""Python Model Management"""

"""
Copyright (c) 2018, EPFL/Blue Brain Project

 This file is part of BluePyMM <https://github.com/BlueBrain/BluePyMM>

 This library is free software; you can redistribute it and/or modify it under
 the terms of the GNU Lesser General Public License version 3.0 as published
 by the Free Software Foundation.

 This library is distributed in the hope that it will be useful, but WITHOUT
 ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
 details.

 You should have received a copy of the GNU Lesser General Public License
 along with this library; if not, write to the Free Software Foundation, Inc.,
 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""


# pylint: disable=C0325, W0223
# pylama: ignore=E402

import sys
import os
import json
import multiprocessing
import multiprocessing.pool
import ipyparallel
import sqlite3
import traceback
import pandas

from bluepymm import tools
from bluepyopt.ephys.responses import TimeVoltageResponse


class TimeVoltageResponseEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, TimeVoltageResponse):
            return obj.response.to_json()
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)


def run_emodel_morph_isolated(input_args):
    """Run e-model morphology combination in isolated environment.

    Args:
        input_args: tuple
        - uid: unique identifier of the e-model morphology combination
        - emodel: e-model name
        - emodel_dir: directory containing e-model files
        - emodel_params: dict that maps e-model parameters to their values
        - morph_path: path to morphology

    Returns:
        Dict with keys 'exception', 'extra_values', 'scores', 'uid'.
    """

    uid, emodel, emodel_dir, emodel_params, morph_path = input_args

    return_dict = {}
    return_dict['uid'] = uid
    return_dict['exception'] = None

    pool = NestedPool(1, maxtasksperchild=1)

    try:
        return_dict['scores'], return_dict['extra_values'] = pool.apply(
            run_emodel_morph, (emodel, emodel_dir, emodel_params, morph_path))
    except Exception:
        return_dict['scores'] = None
        return_dict['extra_values'] = None
        return_dict['exception'] = "".join(traceback.format_exception(
                                           *sys.exc_info()))

    pool.terminate()
    pool.join()
    del pool

    return return_dict


class NoDaemonProcess(multiprocessing.Process):

    """Class that represents a non-daemon process"""

    # pylint: disable=R0201

    def _get_daemon(self):
        """Get daemon flag"""
        return False

    def _set_daemon(self, value):
        """Set daemon flag"""
        pass
    daemon = property(_get_daemon, _set_daemon)


class NestedPool(multiprocessing.pool.Pool):

    """Class that represents a MultiProcessing nested pool"""
    Process = NoDaemonProcess


def read_apical_point(morph_dir, morph_name):
    """Read apical point from apical point json file"""

    json_filename = os.path.join(morph_dir, 'apical_points_isec.json')

    with open(json_filename) as json_file:
        apic_points = json.load(json_file)

    # Get apic_point isec from dict, if not found return None
    if morph_name in apic_points:
        return int(apic_points[morph_name])
    else:
        return None


def run_emodel_morph(
        emodel,
        emodel_dir,
        emodel_params,
        morph_path,
        extra_values_error=True):
    """Run e-model morphology combination.

    Args:
        emodel: e-model name
        emodel_dir: directory containing e-model files
        emodel_params: dict that maps e-model parameters to their values
        morph_path: path to morphology

    Returns:
        tuple:
            - dict that maps features to scores
            - dict with extra values: 'holding_current' and 'threshold_current'
    """

    try:
        sys.stdout = open('/dev/null', 'w')
        print('Running e-model %s on morphology %s in %s' %
              (emodel, morph_path, emodel_dir))

        setup = tools.load_module('setup', emodel_dir)

        print("Changing path to %s" % emodel_dir)
        with tools.cd(emodel_dir):
            if hasattr(setup, 'multieval'):
                apical_point_isec = read_apical_point(
                    os.path.dirname(morph_path), os.path.splitext(
                        os.path.basename(morph_path))[0])

                prefix = '_'

                altmorph = [[prefix, morph_path, apical_point_isec]]
                evaluator = setup.evaluator.create(etype='%s' % emodel,
                                                   altmorph=altmorph)

                evaluator = evaluator.evaluators[0]  # only one evaluator

                responses = evaluator.run_protocols(
                    evaluator.fitness_protocols.values(),
                    emodel_params)

                feature_values = {}
                for objective in evaluator.fitness_calculator.objectives:
                    for feature in objective.features:
                        feature_values[feature.name] = {
                            'value': feature.calculate_feature(responses),
                            'exp_mean': feature.exp_mean,
                            'exp_std': feature.exp_std
                        }

                scores = evaluator.fitness_calculator.calculate_scores(
                    responses)

                extra_values = {}

                for response_key, extra_values_key in [
                        ('%s.bpo_holding_current' % prefix,
                         'holding_current'),
                        ('%s.bpo_threshold_current' % prefix,
                         'threshold_current')]:
                    if response_key in responses:
                        extra_values[extra_values_key] = responses[
                            response_key]
                    else:
                        if extra_values_error:
                            raise ValueError(
                                "Key %s not found in responses: %s" %
                                (response_key, str(responses)))
                        else:
                            extra_values[extra_values_key] = None
            else:
                evaluator = setup.evaluator.create(etype='%s' % emodel)
                evaluator.cell_model.morphology.morphology_path = morph_path

                responses = evaluator.run_protocols(
                    evaluator.fitness_protocols.values(),
                    emodel_params)
                scores = evaluator.fitness_calculator.calculate_scores(
                    responses)

                extra_values = {}
                extra_values['holding_current'] = \
                    responses.get('bpo_holding_current', None)
                extra_values['threshold_current'] = \
                    responses.get('bpo_threshold_current', None)

            traces_fn = 'traces_{}_{}.json'.format(
                emodel, os.path.basename(morph_path))
            traces_path = os.path.join(
                emodel_dir,
                '..',
                '..',
                '..',
                'output',
                'traces')
            try:
                os.mkdir(traces_path)
            except BaseException:
                pass

            traces_fn = os.path.join(traces_path, traces_fn)
            with open(traces_fn, 'w') as traces_file:
                json.dump(
                    responses,
                    traces_file,
                    indent=2,
                    sort_keys=True,
                    cls=TimeVoltageResponseEncoder)

            features_fn = 'features_{}_{}.json'.format(
                emodel, os.path.basename(morph_path))
            features_path = os.path.join(
                emodel_dir,
                '..',
                '..',
                '..',
                'output',
                'features')
            try:
                os.mkdir(features_path)
            except BaseException:
                pass

            features_fn = os.path.join(features_path, features_fn)
            with open(features_fn, 'w') as features_file:
                json.dump(
                    feature_values,
                    features_file,
                    indent=2,
                    sort_keys=True)

        return scores, extra_values
    except Exception:
        # Make sure exception and backtrace are thrown back to parent process
        raise Exception(
            "".join(traceback.format_exception(*sys.exc_info())))


def create_arg_list(scores_db_filename, emodel_dirs, final_dict):
    """Create list of argument tuples to be used as an input for
    run_emodel_morph.

    Args:
        scores_db_filename: path to .sqlite database
        emodel_dirs: a dict mapping e-models to the directories with e-model
            input files
        final_dict: a dict mapping e-models to dicts with e-model parameters

    Raises:
        ValueError, if one of the database entries contains has value None for
        the key 'emodel'.
    """
    arg_list = []

    with sqlite3.connect(scores_db_filename) as scores_db:
        scores_db.row_factory = sqlite3.Row
        scores_cursor = scores_db.execute('SELECT * FROM scores')

        for row in scores_cursor.fetchall():
            index = row['index']
            morph_name = row['morph_name']
            morph_ext = row['morph_ext']

            if morph_ext is None:
                morph_ext = '.asc'

            morph_filename = morph_name + morph_ext
            morph_path = os.path.abspath(os.path.join(row['morph_dir'],
                                                      morph_filename))
            if row['to_run'] == 1:
                emodel = row['emodel']
                original_emodel = row['original_emodel']
                if emodel is None:
                    raise ValueError(
                        "scores db row %s for morph %s, etype %s, mtype %s, "
                        "layer %s doesn't have an e-model assigned to it" %
                        (index, morph_name, row['etype'], row['mtype'],
                         row['layer']))
                args = (index, emodel,
                        os.path.abspath(emodel_dirs[emodel]),
                        final_dict[original_emodel]['params'],
                        morph_path)
                arg_list.append(args)

    print('Found %d rows in score database to run' % len(arg_list))

    return arg_list


def save_scores(scores_db_filename, uid, scores, extra_values, exception,
                float_representation='.17g'):
    """Update a specific entry in a given database with scores and related
    parameters.

    Args:
        scores_db_filename: path to .sqlite database
        uid: unique identifier of database entry
        scores: scores dict to be added to entry as a json string
        extra_values: dict to be added to entry as a json string
        exception: description of exception that may have happened during score
                   calculation
        float_representation: use for json encoding. Default is '.17g'.

    Returns:
        ValueError if entry has already been updated.
    """
    json.encoder.FLOAT_REPR = lambda x: format(x, float_representation)

    with sqlite3.connect(scores_db_filename) as scores_db:
        # make sure we don't update a row that was already executed
        scores_cursor = scores_db.execute(
            'SELECT `index` FROM scores WHERE `index`=? AND to_run=?',
            (uid, False))
        if scores_cursor.fetchone() is None:
            # update row with calculated scores and related values
            scores_db.execute('UPDATE scores SET scores=?, extra_values=?, '
                              'exception=?, to_run=? WHERE `index`=?',
                              (json.dumps(scores), json.dumps(extra_values),
                               exception, False, uid))
        else:
            raise ValueError('save_scores: trying to update scores in a row '
                             'that was already executed: %d' % uid)


def expand_scores_to_score_values_table(scores_sqlite_filename):
    """Read scores from sqlite table, expand to dataframe, and store in new
    table 'score_values'. Each column of the new table corresponds to a
    single score.

    Args:
        scores_sqlite_filename: path to sqlite database with keys 'scores' and
                                'to_run'

    Raises:
        Exception, if the scores table contains at least one entry where the
        value of 'to_run' is True.
    """
    with sqlite3.connect(scores_sqlite_filename) as conn:
        scores = pandas.read_sql('SELECT * FROM scores', conn)
        tools.check_all_combos_have_run(scores, 'scores')

    score_values = scores['scores'].apply(
        lambda json_str: pandas.Series
        (json.loads(json_str)) if json_str else pandas.Series())

    scores.to_csv('output/scores.csv')
    score_values.to_csv('output/score_values.csv')

    with sqlite3.connect(scores_sqlite_filename) as conn:
        score_values.to_sql('score_values', conn, if_exists='replace',
                            index=False)


def calculate_scores(final_dict, emodel_dirs, scores_db_filename,
                     use_ipyp=False, ipyp_profile=None):
    """Calculate scores of e-model morphology combinations and update the
    database accordingly.

    Args:
        scores_db_filename: path to .sqlite database with e-model morphology
            combinations
        final_dict: a dict mapping e-models to dicts with e-model parameters
        emodel_dirs: a dict mapping e-models to the directories with e-model
            input files
        use_ipyp: bool indicating whether ipyparallel is used. Default is
            False.
        ipyp_profile: path to ipyparallel profile. Default is None.
    """

    print('Creating argument list for parallelisation')
    arg_list = create_arg_list(scores_db_filename, emodel_dirs, final_dict)

    print('Parallelising score evaluation of %d me-combos' % len(arg_list))

    if use_ipyp:
        # use ipyparallel
        client = ipyparallel.Client(profile=ipyp_profile)
        lview = client.load_balanced_view()
        results = lview.imap(run_emodel_morph_isolated,
                             arg_list, ordered=False)
    else:
        # use multiprocessing
        pool = NestedPool()
        results = pool.imap_unordered(run_emodel_morph_isolated, arg_list)

    # keep track of the number of received results
    uids_received = 0

    # every time a result comes in, save the score in the database
    for result in results:
        uid = result['uid']
        scores = result['scores']
        extra_values = result['extra_values']
        exception = result['exception']
        uids_received += 1

        save_scores(scores_db_filename, uid, scores, extra_values, exception)

        print('Saved scores for uid %s (%d out of %d) %s' %
              (uid, uids_received, len(arg_list),
               'with exception: {}'.format(exception) if exception else ''))
        sys.stdout.flush()

    print('Converting score json strings to scores values ...')
    expand_scores_to_score_values_table(scores_db_filename)

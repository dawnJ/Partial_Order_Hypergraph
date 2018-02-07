import argparse
import tensorflow as tf
import numpy as np
from time import time
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(sys.path[0], '..')))
from util.util_io import calc_partial_order_incidence, \
    calc_hyper_laplacian_similarity, cur_print_performance
from evaluator import Evaluator, FoldEvaluator
import copy


class PartialOrderHypergraph():
    def __init__(self, feature_fname, origin_rank_fname, partial_order_fnames,
                 order_directions, model_parameters, evaluator, verbose=True):
        assert len(partial_order_fnames) == 1 \
               or len(partial_order_fnames) == 2, 'for more partial orders, ' \
                                                  'try xxxx.py'
        self.random_seed = int(time())
        self.parameters = copy.copy(model_parameters)
        self.evaluator = evaluator
        self.verbose = verbose
        self.order_directions = order_directions

        self.feature = np.genfromtxt(feature_fname, dtype=float, delimiter=',')
        print 'feature shape:', self.feature.shape

        self.y = np.matrix(np.genfromtxt(origin_rank_fname, delimiter=',',
                                         dtype=float)).T
        print 'origin ranking shape:', self.y.shape
        self.n = self.y.shape[0]

        # read in partial orders (kind of heuristic rankings), calculate
        # hypergraph Laplacian matrix and partial order incidence matrix
        self.partial_orders = []
        for partial_order_fname in partial_order_fnames:
            self.partial_orders.append(np.genfromtxt(partial_order_fname,
                                                     dtype=float,
                                                     delimiter=','))
        self.H = []     # Incidence matrix of conventional hypergraph
        self.L = []     # Laplacian matrix of conventional hypergraph
        self.Hs = []    # partial order incidence matrices
        self.L, self.H = calc_hyper_laplacian_similarity(
            self.feature, self.parameters['k'], False, return_H=True
        )
        self.Hs = []
        for i in range(len(partial_order_fnames)):
            self.Hs.append(copy.copy(calc_partial_order_incidence(
                    self.partial_orders[i],
                    self.parameters['drop_ratio_' + str(i)],
                    self.order_directions[i], self.H))
            )

    def _init_graph(self):
        self.graph = tf.Graph()
        with self.graph.as_default():
            tf.set_random_seed(self.random_seed)
            self.L_tf = tf.placeholder(tf.float32, shape=[self.n, self.n])
            self.y_tf = tf.placeholder(tf.float32, shape=[self.n, 1])
            self.all_one_tf = tf.placeholder(tf.float32, shape=[self.n, 1])

            self.f_tf = tf.Variable(initial_value=self.y, dtype=tf.float32,
                                    expected_shape=[self.n, 1])
            self.Hs_tf = [tf.placeholder(tf.float32, shape=[self.n, self.n])
                          for _ in xrange(len(self.partial_orders))]
            self._init_graph_loss()
            self._init_partial_order_loss()
            self._init_overall_loss()

            optimizer_type = self.parameters['optimizer_type']
            learning_rate = self.parameters['learning_rate']
            if optimizer_type == 'adam':
                self.optimizer = tf.train.AdamOptimizer(
                    learning_rate=learning_rate, beta1=0.9, beta2=0.999,
                    epsilon=1e-8).minimize(self.overall_loss)
            elif optimizer_type == 'adag':
                self.optimizer = tf.train.AdagradOptimizer(
                    learning_rate=learning_rate,
                    initial_accumulator_value=1e-8).minimize(self.overall_loss)
            elif optimizer_type == 'gd':
                self.optimizer = tf.train.GradientDescentOptimizer(
                    learning_rate=learning_rate).minimize(
                    self.overall_loss)
            elif optimizer_type == 'mom':
                self.optimizer = tf.train.MomentumOptimizer(
                    learning_rate=learning_rate, momentum=0.95).minimize(
                    self.overall_loss)

            init = tf.global_variables_initializer()
            self.sess = tf.Session()
            self.sess.run(init)

    def _init_graph_loss(self):
        self.historical_ranking_loss = tf.reduce_sum(
            tf.nn.l2_loss(tf.subtract(self.y_tf, self.f_tf))
        ) * tf.cast(self.parameters['lam_i'], tf.float32)
        self.manifold_ranking_loss = tf.reduce_sum(
            tf.matmul(tf.matmul(self.f_tf, self.L_tf, transpose_a=True),
                      self.f_tf))
        self.graph_loss = self.historical_ranking_loss \
                          + self.manifold_ranking_loss

    def _init_partial_order_loss(self):
        pair_wise_difference = tf.subtract(
            tf.matmul(self.f_tf, self.all_one_tf, transpose_b=True),
            tf.matmul(self.all_one_tf, self.f_tf, transpose_b=True)
        )
        pair_wise_difference = tf.multiply(pair_wise_difference,
                                           tf.cast(-1.0, tf.float32))
        self.partial_order_loss = 0.0
        for i in xrange(len(self.partial_orders)):
            if i == 0:
                self.partial_order_loss += (tf.reduce_sum(tf.nn.relu(
                        tf.multiply(self.Hs_tf[i], pair_wise_difference))
                ) * tf.cast(self.parameters['alpha'], tf.float32))
            elif i == 1:
                self.partial_order_loss += (tf.reduce_sum(tf.nn.relu(
                    tf.multiply(self.Hs_tf[i], pair_wise_difference))
                ) * tf.cast(1 - self.parameters['alpha'], tf.float32))
            else:
                print 'for more partial orders, try xxxx.py'
                exit()
        self.partial_order_loss *= tf.cast(self.parameters['beta'],
                                           tf.float32)

    def _init_overall_loss(self):
        self.overall_loss = self.partial_order_loss + self.graph_loss

    def ranking(self):
        self._init_graph()
        best_performance = {'tau': [-1.0, 7.0e-05], 'mse': 1e3,
                            'mae': 1e3, 'rho': [-1.0, 9.2e-05]}
        best_generated_ranking = []
        best_epoch = 0
        t0 = time()
        for epoch in xrange(self.parameters['total_epoch']):
            # train
            t1 = time()
            feed_dict = {H_tf: H for H_tf, H in zip(self.Hs_tf, self.Hs)}
            feed_dict[self.L_tf] = self.L
            feed_dict[self.y_tf] = self.y
            feed_dict[self.all_one_tf] = np.matrix(
                np.ones(self.n, dtype=float)).T
            generated_ranking, overall_loss, historical_loss, manifold_loss, \
            graph_loss, partial_order_loss, batch_out = self.sess.run(
                (self.f_tf, self.overall_loss, self.historical_ranking_loss,
                 self.manifold_ranking_loss, self.graph_loss,
                 self.partial_order_loss, self.optimizer),
                feed_dict=feed_dict)
            t2 = time()

            # evaluate
            current_performance = self.evaluator.evaluate(generated_ranking)
            is_better = self.evaluator.compare(current_performance,
                                               best_performance)
            if self.verbose:
                print '\tEpoch: %04d; Time: %.4f; Loss: %.4f = %.4f + %.4f + %.4f'\
                    % (epoch, t2 - t1, overall_loss, historical_loss,
                       manifold_loss, partial_order_loss)
            if is_better['tau']:
                best_performance = copy.copy(current_performance)
                if self.verbose:
                    print 'better TAU performance:', current_performance
                best_generated_ranking = copy.copy(generated_ranking)
                best_epoch = epoch
            else:
                if self.verbose:
                    print 'current performance:', current_performance
        print 'time:', time() - t0
        if self.verbose:
            print '@epoch %d best performance:' % best_epoch, best_performance
        if best_epoch + 1 == self.parameters['total_epoch']:
            print 'not converge yet'
        self.sess.close()
        return best_generated_ranking

    def update_model(self, model_parameters):
        # need to construct new Laplacian matrix
        if 'k' in model_parameters.keys() and (not model_parameters['k'] ==
            self.parameters['k']):
            # update the Laplacian and incidence matrix
            if model_parameters['k'] > self.n - 1:
                return False
            self.L, self.H = calc_hyper_laplacian_similarity(
                self.feature, model_parameters['k'], False, return_H=True
            )
            for i in range(len(self.order_directions)):
                self.Hs[i] = copy.copy(calc_partial_order_incidence(
                    self.partial_orders[i],
                    model_parameters['drop_ratio_' + str(i)],
                    self.order_directions[i], self.H))
        else:
            for i in range(len(self.order_directions)):
                drop_ratio_key = 'drop_ratio_' + str(i)
                if drop_ratio_key in model_parameters.keys() and (
                        not model_parameters[drop_ratio_key] ==
                            self.parameters[drop_ratio_key]):
                    self.Hs[i] = copy.copy(
                            calc_partial_order_incidence(
                                    self.partial_orders[i],
                                    model_parameters[drop_ratio_key],
                                    self.order_directions[i], self.H
                            )
                    )

        # update model parameters
        for parameter_kv in model_parameters.iteritems():
            self.parameters[parameter_kv[0]] = parameter_kv[1]
        return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', '--type', type=str, default='salary',
                        help='denote the type of partial-order relation')
    args = parser.parse_args()
    if args.type == 'salary':
        parameter = {'lam_i': 2e2, 'beta': 6e-3, 'alpha': 1.0, 'k': 200,
                     'drop_ratio_0': 0.05, 'drop_ratio_1': 0.05,
                     'learning_rate': 1e-6, 'total_epoch': 50,
                     'optimizer_type': 'mom'}
        ofname = 'poh_salary.rank'
    elif args.type == 'ncee':
        parameter = {'lam_i': 2e2, 'beta': 6e-2, 'alpha': 1.0, 'k': 200,
                     'drop_ratio_0': 0.05, 'drop_ratio_1': 0.05,
                     'learning_rate': 5e-7, 'total_epoch': 50,
                     'optimizer_type': 'mom'}
        ofname = 'poh_ncee.rank'
    elif args.type == 'all':
        parameter = {'lam_i': 2e2, 'beta': 8e-1, 'alpha': 0.6, 'k': 200,
                     'drop_ratio_0': 0.05, 'drop_ratio_1': 0.05,
                     'learning_rate': 1e-8, 'total_epoch': 50,
                     'optimizer_type': 'mom'}
        ofname = 'poh_all.rank'

    feature_path = os.path.join(sys.path[0], 'data', 'cur')
    fold_count = 5
    partial_order_fnames = [
        os.path.join(feature_path, 'heuristic_ranking_ipin_salary_list.csv'),
        os.path.join(feature_path, 'heuristic_ranking_entrance_line_list.csv')
    ]
    fold_evaluate = FoldEvaluator(
        os.path.join(feature_path, 'ground_truth.csv'), fold_count
    )
    evaluate = Evaluator(os.path.join(feature_path, 'ground_truth.csv'))
    rerank_tf = PartialOrderHypergraph(
        os.path.join(feature_path, 'feature_all_nmf_0.02.csv'),
        os.path.join(feature_path, 'ranking_aver_2015_list.csv'),
        partial_order_fnames, [False, False], parameter, evaluate, False
    )
    generated_ranking = rerank_tf.ranking()
    np.savetxt(ofname, generated_ranking, fmt='%.8f')
    mae = 0.0
    tau = 0.0
    rho = 0.0
    for i in xrange(fold_count):
        performance = fold_evaluate.single_fold_evaluate(generated_ranking, i)
        mae += performance['mae']
        tau += performance['tau'][0]
        rho += performance['rho'][0]
        print '-------------------------'
        cur_print_performance(performance)
    print 'average performance'
    print 'mae:', mae / fold_count, 'tau:', tau / fold_count, 'rho:', \
        rho / fold_count
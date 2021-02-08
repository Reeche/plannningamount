import json
import os
from functools import partial

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyabc
import seaborn as sns
from models.hierarchical_models import HierarchicalLearner
from hyperopt import hp, fmin, tpe, Trials
from utils.learning_utils import compute_objective, get_relevant_data
from models.lvoc_models import LVOC
from pyabc.transition import MultivariateNormalTransition
from models.reinforce_models import REINFORCE, BaselineREINFORCE
from models.rssl_models import RSSL
from models.sdss_models import SDSS
from env.modified_mouselab import get_termination_mers

models = {'lvoc': LVOC, 'rssl': RSSL, 'hierarchical_learner': HierarchicalLearner,
          'sdss': SDSS, 'reinforce': REINFORCE, 'baseline_reinforce': BaselineREINFORCE}

curr_dir = os.path.abspath(os.path.dirname(__file__))
param_config = json.load(open(os.path.join(curr_dir, "param_search_space.json")))
model_config = json.load(open(os.path.join(curr_dir, "model_params.json")))
model_details = json.load(open(os.path.join(curr_dir, "../models/models.json")))

def hyperopt_space(params_list):
    """Should return a dict of the form required by hyperopt
    
    Arguments:
        params {[list]} -- List of param configs
    """
    space = {}
    for param, param_type, param_range in params_list:
        if param_type != "constant":
            a = param_range[0]
            b = param_range[1]
            if param_type == "uniform":
                space[param] = hp.uniform(param, a, b)
            elif param_type == "loguniform":
                # space[param] = hp.loguniform(param, np.log(a), np.log(b)) # Verify this
                # This change is to maintain uniformity
                space[param] = hp.uniform(param, np.log(a), np.log(b))
            elif param_type == "quniform":
                space[param] = hp.quniform(param, a, b, 1)
            elif param_type == "normal":
                space[param] = hp.normal(param, a, b)
        else:
            space[param] = param_range
    space['lik_sigma'] = hp.uniform('lik_sigma', np.log(1e-3), np.log(1e3))
    return space

def pyabc_prior(params_list):
    """Should return a dict of the form required by pyabc
    
    Arguments:
        params {[list]} -- List of param configs
    """
    prior = {}
    for param, param_type, param_range in params_list:
        if param_type != "constant":
            a = param_range[0]
            b = param_range[1]
            if param_type == "uniform" or param_type == "quniform":
                # pyabc does not yet support discrete uniform well
                prior[param] = pyabc.RV("uniform", a, b-a)
            elif param_type == "loguniform":
                #prior[param] = pyabc.RV("loguniform", *param_range)
                log_param_range = np.log(param_range)
                prior[param] = pyabc.RV("uniform", log_param_range[0], log_param_range[1]-log_param_range[0])
            elif param_type == "normal":
                prior[param] = pyabc.RV("norm", *param_range)
        else:
            prior[param] = pyabc.RV("uniform", param_range, 0)
    prior = pyabc.Distribution(**prior)
    return prior

def param_info(param_dict, key):
    return (key, param_dict["type"], param_dict["range"])

def get_params_list(params_dict):
    params_list = []
    for param in params_dict:
        if params_dict[param]:
            params_list.append(param_info(params_dict[param], param))
    return params_list

def get_params(params, param_config):
    params_list = []
    for param in params:
        params_list.append(param_info(param_config["model_params"][param], param))
    return params_list

def make_constant(constant_value):
    return {'type': 'constant', 'range': constant_value}

def make_prior(param_dict, num_priors, bandit_prior=False):
    params_list = []
    t = "prior"
    if bandit_prior:
        t = "bandit_prior"
    for i in range(num_priors):
        params_list.append(param_info(param_dict, f"{t}_{i}"))
    return params_list

def parse_config(learner, learner_attributes, hierarchical=False,
                hybrid=False, general_params=False):
    params_list = []
    bandit_prior = False
    learner_params = model_config[learner]
    param_models = param_config["model_params"]

    # Add base params
    params_list += get_params(learner_params["params"], param_config)

    # Adding extra params if they are in attributes and have a value True
    extra_params = learner_params["extra_params"]
    for i, param in enumerate(extra_params):
        if param in learner_attributes and learner_attributes[param]:
            params_list.append(param_info(param_models[param], param))
        # else:
        #     con = learner_params["extra_param_defaults"][i]
        #     params_list.append(param_info(make_constant(con), param))

    # General params
    if general_params:
        if 'pr_weight' in learner_attributes:
            params_list.append(param_info(param_models["pr_weight"], "pr_weight"))
        else:
            params_list.append(param_info(make_constant(1), 'pr_weight'))
    
    if hierarchical:
        decision_rule = learner_attributes['decision_rule']
        actor = learner_attributes['actor']
        params_list += get_params_list(param_config["decision_params"][decision_rule])
        params_list += parse_config(actor, learner_attributes, False, False, False)
    elif hybrid:
        selector = learner_attributes['selector']
        learner = learner_attributes['learner']
        # if 'bandit_prior' in learner_attributes:
        #     bandit_prior = learner_attributes['bandit_prior']
        #     num_bandit_priors = learner_attributes['num_bandit_priors']
        #     param_dict = param_models[bandit_prior]
        #     params_list += make_prior(param_dict, num_bandit_priors, True)
        params_list += parse_config(selector, learner_attributes, False, False, False)
        params_list += parse_config(learner, learner_attributes, False, False, False)
    else:
        if 'prior' in learner_attributes:
            prior = learner_attributes['prior']
            num_priors = learner_attributes['num_priors']
            param_dict = param_models[prior]
            params_list += make_prior(param_dict, num_priors, False)
    return params_list

def get_space(learner, learner_attributes, optimizer="pyabc"):
    hierarchical = False
    hybrid = False
    if learner == 'hierarchical_learner':
        hierarchical=True
    if learner == "sdss":
        hybrid = True
    params_list = parse_config(learner, learner_attributes, hierarchical, hybrid, True)
    if optimizer == "pyabc":
        return pyabc_prior(params_list)
    else:
        return hyperopt_space(params_list)

def construct_p_data(participant, pipeline):
    p_data = {
        'envs': participant.envs,
        'a' : participant.clicks,
        's': participant.strategies,
        'mer': get_termination_mers(participant.envs, participant.clicks, pipeline),
        'r': participant.scores,
        'w': participant.weights
    }
    return p_data

def construct_objective_fn(optimizer, objective, p_data, pipeline):
    objective_fn = lambda x, y: compute_objective(objective, x, p_data, pipeline)
    if optimizer == "pyabc":
        if objective in ["reward", "strategy_accuracy", "clicks_overlap"]:
            objective_fn = lambda x, y: -compute_objective(objective, x, y, pipeline)
        else:
            objective_fn = lambda x, y: compute_objective(objective, x, y, pipeline)
    return objective_fn

def optimize_hyperopt_params(objective_fn, param_ranges, max_evals=100,
                            trials=True, method=tpe.suggest, init_evals=30,
                            show_progressbar=False):
    estimator = partial(method, n_startup_jobs=init_evals)
    trials = Trials() if trials else None
    best_params = fmin(fn=objective_fn, space=param_ranges,
                        algo=estimator, max_evals=max_evals, trials=trials,
                        show_progressbar=show_progressbar)
    return best_params, trials

def estimate_pyabc_posterior(model, prior, distance_fn, observation, 
                            db_path, eps=0.1, num_populations=10):
    """
        See if this can be made to use the model selection function
    """
    transition = MultivariateNormalTransition(scaling=0.1)
    abc = pyabc.ABCSMC(model, prior, distance_fn, transitions=[transition],
                        population_size=20) # Change this
    abc.new(db_path, observation)
    history = abc.run(minimum_epsilon=eps, max_nr_populations=num_populations)
    return history

def combine_priors(params, num_priors, prefix="prior"):
    init_weights = np.zeros(num_priors)
    for i in range(num_priors):
        init_weights[i] = params[f"{prefix}_{i}"]
    return init_weights

class ParameterOptimizer:
    def __init__(self, learner, learner_attributes, participant, env):
        self.learner = learner
        self.learner_attributes = learner_attributes
        self.participant = participant
        self.env = env
        self.pipeline = self.env.pipeline
        self.compute_likelihood=False
        if self.learner in ['sdss']:
            self.model = models[self.learner_attributes['learner']]
        elif self.learner in ['hierarchical_learner']:
            self.model = models[self.learner_attributes['actor']]
        self.reward_data = []

    def objective_fn(self, params, get_sim_data=False):
        features = self.learner_attributes['features']
        num_priors = self.learner_attributes['num_priors']
        priors = combine_priors(params, num_priors)
        params['priors'] = priors
        if self.learner == "sdss":
            num_strategies = int(params['num_strategies'])
            bandit_params = np.ones(2*num_strategies)
            bandit_params[:num_strategies] *= params['alpha']
            bandit_params[num_strategies:] *= params['beta']
            params['bandit_params'] = bandit_params
            self.learner_attributes['learner'] = self.model
            self.learner_attributes['strategy_space'] = list(range(num_strategies))
        elif self.learner == "hierarchical_learner":
            self.learner_attributes['actor'] = self.model
        agent = models[self.learner](params, self.learner_attributes)
        del params['priors']
        if self.learner == "sdss":
            del params['bandit_params']
        simulations_data = agent.run_multiple_simulations(self.env, self.num_simulations,
                participant=self.participant, compute_likelihood=self.compute_likelihood)
        relevant_data = get_relevant_data(simulations_data, self.objective)
        if self.objective in ["mer_performance_error", "pseudo_likelihood"]:
            self.reward_data.append(relevant_data["mer"])
        if self.objective == "pseudo_likelihood":
            relevant_data['sigma'] = params['lik_sigma']
        if get_sim_data:
            return relevant_data, simulations_data
        else:
            return relevant_data

    def get_prior(self):
        return get_space(self.learner, self.learner_attributes, self.optimizer)

    def optimize(self, objective, num_simulations=1, optimizer="pyabc",
                db_path = "sqlite:///test.db", compute_likelihood=False,
                max_evals=100):
        self.objective = objective
        self.compute_likelihood = compute_likelihood
        self.num_simulations = num_simulations
        self.optimizer = optimizer
        prior = self.get_prior()
        p_data = construct_p_data(self.participant, self.pipeline)
        self.p_data = p_data
        distance_fn = construct_objective_fn(optimizer, objective, p_data, self.pipeline)
        observation = get_relevant_data(p_data, self.objective)
        if optimizer == "pyabc":
            res = estimate_pyabc_posterior(self.objective_fn, prior, distance_fn, observation,
                        db_path, num_populations=5)
        else:
            objective_fn = lambda x: distance_fn(self.objective_fn(x), p_data)
            res = optimize_hyperopt_params(objective_fn, prior, max_evals=max_evals,
                                            show_progressbar=True)
        return res, prior, self.objective_fn

    def run_model(self, params, objective, num_simulations=1, optimizer="pyabc", 
                 db_path="sqlite:///test.db"):
        self.objective = objective
        self.num_simulations = num_simulations
        p_data = construct_p_data(self.participant, self.pipeline)
        data = self.objective_fn(params)
        return data, p_data

    def run_hp_model(self, params, objective, num_simulations=1):
        self.objective = objective
        self.num_simulations = num_simulations
        p_data = construct_p_data(self.participant, self.pipeline)
        data = self.objective_fn(params, get_sim_data=True)
        return data, p_data

    def run_hp_model_nop(self, params, objective, num_simulations=1):
        self.objective = objective
        self.num_simulations = num_simulations
        #p_data = construct_p_data(self.participant, self.pipeline)
        p_data = {}
        data = self.objective_fn(params, get_sim_data=True)
        return data, p_data

    def plot_rewards(self, i=0, path=""):
        data = []
        # for i in range(len(self.reward_data)):
        for j in range(len(self.reward_data[i])):
            for k in range(len(self.reward_data[i][j])):
                data.append([k+1, self.reward_data[i][j][k], "algo"])
        p_mer = self.p_data["mer"]
        for i, m in enumerate(p_mer):
            data.append([i+1, m, "participant"])
        reward_data = pd.DataFrame(data, columns=["x", "y", "algo"])
        ax = sns.lineplot(x="x", y="y", hue = "algo", data=reward_data)
        plt.savefig(path, bbox_inches='tight')
        plt.show()
        return reward_data

    def plot_history(self, history, prior, obj_fn):
        # fig, ax = plt.subplots()
        # for t in range(0,history.max_t+1):
        #     df, w = history.get_distribution(m=0, t=t)
        #     pyabc.visualization.plot_kde_1d(df, w,
        #                                 x="", ax=ax,
        #                                 label="PDF t={}".format(t))
        # plt.legend()
        # plt.show()

        posterior = pyabc.transition.MultivariateNormalTransition()
        posterior.fit(*history.get_distribution(m=0))

        sim_prior_params = []
        sim_posterior_params = []
        prior_rewards = []
        posterior_rewards = []
        num_simulations = 100

        for i in range(num_simulations):
            prior_params = prior.rvs()
            sim_prior_params.append(prior_params)
            prior_sample = obj_fn(prior_params)
            prior_rewards.append(prior_sample["mer"][0])

            posterior_params = posterior.rvs()
            sim_posterior_params.append(posterior_params)
            posterior_sample = obj_fn(posterior_params)
            posterior_rewards.append(posterior_sample["mer"][0])
        
        mean_prior_rewards = np.mean(prior_rewards, axis=0)
        mean_posterior_rewards = np.mean(posterior_rewards, axis=0)
        plt.plot(mean_prior_rewards, label = 'Prior')
        plt.plot(mean_posterior_rewards, label = 'Posterior')
        plt.plot(self.participant.scores, label = 'Participant')
        plt.legend()
        plt.show()

        return history

def plot_model_selection_results(run_history, model_names):
    _, ax = plt.subplots(figsize=(10, 7))
    model_probs = run_history.get_model_probabilities()
    model_probs.columns = [model_names[c] for c in model_probs.columns]
    print(model_probs.to_string())
    ax = model_probs.plot.bar(legend=True, ax=ax)
    ax.set_ylabel("Probability")
    ax.set_xlabel("Population index")
    plt.show()
    plt.show()

class BayesianModelSelection:
    def __init__(self, models_list, model_attributes, participant, env, 
                objective, num_simulations):
        self.optimizers = []
        self.models = []
        self.participant = participant
        self.env = env
        self.pipeline = env.pipeline
        self.objective = objective
        for i, model in enumerate(models_list):
            self.models.append(model)
            optimizer = ParameterOptimizer(model, model_attributes[i], participant, env)
            optimizer.num_simulations = num_simulations
            optimizer.objective = objective
            optimizer.optimizer = "pyabc"
            self.optimizers.append(optimizer)
        self.num_models = len(models_list)
    
    def model_selection(self):
        priors = []
        models = []
        for opt in self.optimizers:
            models.append(opt.objective_fn)
            priors.append(opt.get_prior())
        p_data = construct_p_data(self.participant, self.pipeline)
        observation = get_relevant_data(p_data, self.objective)
        distance_fn = construct_objective_fn("pyabc", self.objective, p_data, self.pipeline)
        transitions = [MultivariateNormalTransition(scaling=0.1) for _ in range(self.num_models)]
        abc = pyabc.ABCSMC(models, priors, distance_fn, transitions=transitions,
                            population_size=100)
        db_path = ("sqlite:///" + "test.db")
        abc.new(db_path, observation)
        history = abc.run(max_nr_populations=5)
        return history

import argparse
from datetime import datetime
from glob import glob
import subprocess
from typing import AnyStr, List, Tuple
from Utils.DB.DBUtils import PGRunner, ISQLRunner
from Utils.DB.QueryUtils import Query
import numpy as np
from itertools import count
from math import log
import random
import time
from Utils.Model.DQN import DQN,ENV
from Utils.Model.TreeLSTM import SPINN
from Utils.Parser.JOBParser import DB
import copy
import torch
from torch.nn import init
import os
from tqdm import tqdm
import graphviz as gv
import pandas as pd
import logging
import yaml
import ast

class CostTraining:
    def __init__(self, config: dict) -> None:

        self.config = config
        self.handlers = []
        log_level = "DEBUG"
        if config['logging']['debug'] == 2:
            self.handlers.append(logging.FileHandler(os.path.join(
                "models",
                config["model"]["name"],
                f'cost-training_{config["model"]["name"]}.log'
            )))
        elif config['logging']['debug'] == 1:
            self.handlers.append(logging.StreamHandler())
        else:
            log_level = "ERROR"

        logging.basicConfig(
            level=log_level,
            handlers=self.handlers,
            format='%(asctime)s - %(message)s',
            datefmt='%m/%d/%Y %I:%M:%S'
        )

        self.device = torch.device("cuda" if config['model']['device'] == "gpu" and torch.cuda.is_available() else "cpu")

        with open(self.config["database"]["pg_schema_file"], "r") as f:
            createSchema = "".join(f.readlines())

        self.db_info = DB(createSchema, config=config)

        self.featureSize = self.config["model"]["feature_size"]

        self.policy_net = SPINN(
            n_classes = 1, size = self.featureSize, 
            n_words = self.config["model"]["n_words"],
            mask_size= len(self.db_info)*len(self.db_info),
            device=self.device, 
            max_column_in_table=self.config["model"]["max_column_in_table"]
        ).to(self.device)

        self.target_net = SPINN(
            n_classes = 1, size = self.featureSize, 
            n_words = self.config["model"]["n_words"],
            mask_size= len(self.db_info)*len(self.db_info),
            device=self.device, 
            max_column_in_table=self.config["model"]["max_column_in_table"]
        ).to(self.device)

        for name, param in self.policy_net.named_parameters():
            logging.debug(f"Parameter: {name} of shape {param.shape}")
            if len(param.shape)==2:
                init.xavier_normal_(param)
            else:
                init.uniform_(param)

        self.checkpoint = None

        if not os.path.exists(os.path.join("models", self.config["model"]["name"], self.config['model']['checkpoint'])):
            self.checkpoint = dict({
                "checkpoint": 0,
                "best_model": 0,
                "best_loss": np.inf,
                "latest_model": os.path.join("models", self.config["model"]["name"], "CostTraining.pth")
            })
        else:
            self.checkpoint = yaml.load(
                open(
                    os.path.join("models", self.config["model"]["name"], self.config['model']['checkpoint']), 
                    mode='r'
                ), 
                Loader=yaml.FullLoader
            )

        if os.path.exists(self.checkpoint["latest_model"]):
            self.policy_net.load_state_dict(torch.load(self.checkpoint["latest_model"], map_location=self.device))
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()

        self.runner = (
            PGRunner(
                config["database"]['pg_dbname'],
                config["database"]['pg_user'],
                config["database"]['pg_password'],
                config["database"]['pg_host'],
                config["database"]['pg_port'],
                isCostTraining=True,
                latencyRecord = False,
                latencyRecordFile = "Cost.json"
            ) if config["database"]["engine_class"] == "sql" else
            ISQLRunner(
                config["database"][f'{config["database"]["engine_name"]}_endpoint'],
                config["database"][f'{config["database"]["engine_name"]}_graph'],
                config["database"][f'{config["database"]["engine_name"]}_host'],
                config["database"][f'{config["database"]["engine_name"]}_port'],
                client=config["database"]["engine_name"],
                isCostTraining=True,
                latencyRecord = False,
                latencyRecordFile = "Cost.json"
            )
        )

        self.dqn = DQN(self.policy_net,self.target_net,self.db_info,self.runner, self.device, config=config)

    def k_fold(self, input_list: List[Query],k,ix = 0) -> Tuple[List[Query], List[Query]]:
        li = len(input_list)
        kl = (li-1)//k + 1
        train = []
        validate = []
        for idx in range(li):
            if idx%k == ix:
                validate.append(input_list[idx])
            else:
                train.append(input_list[idx])
        return train, validate


    def QueryLoader(self, QueryDir: str) -> List[Query]:
        def file_name(file_dir):
            import os
            L = []
            for root, dirs, files in os.walk(file_dir):
                for file in files:
                    if os.path.splitext(file)[1] == f'.{ self.config["database"]["engine_class"]}':
                        L.append(os.path.join(root, file))
            return L
        files = file_name(QueryDir)
        sql_list = []
        for filename in files:
            with open(filename, "r") as f:
                data = f.readlines()
                one_sql = "".join(data)
                sql_list.append(Query(self.runner,one_sql,filename))
        return sql_list

    def resample_sql(self, sql_list: List[Query]):
        rewards = []
        reward_sum = 0
        rewardsP = []
        mes = 0
        for sql in sql_list:
            env = ENV(sql,self.db_info,self.runner,self.device, self.config)

            for t in count():
                action_list, chosen_action, all_action = self.dqn.select_action(env,need_random=False)

                left = chosen_action[0]
                right = chosen_action[1]
                env.takeAction(left,right)

                prediction, cost, reward, done = env.reward()
                if done:
                    mrc = reward
                    rewardsP.append(mrc)
                    mes += mrc
                    rewards.append((mrc,sql))
                    reward_sum += mrc
                    break
        import random
        logging.debug(rewardsP)
        res_sql = []
        logging.debug(mes/len(sql_list))
        for idx in range(len(sql_list)):
            rd = random.random()*reward_sum
            for ts in range(len(sql_list)):
                rd -= rewards[ts][0]
                if rd<0:
                    res_sql.append(rewards[ts][1])
                    break
        return res_sql+sql_list

    def train(self, trainSet: List[Query], validateSet: List[Query], n_episodes=10000):

        trainSet_temp: List[Query] = np.array(trainSet)
        losses = []
        avg_eps_losses = []
        startTime = time.time()
        training_summary = None
        validation_summary = None
        last_validation_loss = None
        best_episode = None

        for i_episode in tqdm(range(0,n_episodes), unit="episode"):
            
            if i_episode < self.checkpoint['checkpoint']: continue

            avg_eps_losses = []

            # if i_episode % self.config["model"]["shuffle_train_every"] == 100:
            #     logging.debug("Resampling training set...")
            #     trainSet = self.resample_sql(trainSet_temp)
            # sqlt = random.sample(trainSet[0:],1)[0]

            if i_episode % config["model"]["shuffle_train_every"] == 0:
                np.random.shuffle(trainSet_temp)

            for qidx, sqlt in enumerate(tqdm(trainSet_temp, unit="query")):
                env = ENV(sqlt,self.db_info,self.runner,self.device, self.config)

                if config["logging"]["use_graphviz"]:
                    format = os.environ['RTOS_GV_FORMAT'] if os.environ.get('RTOS_GV_FORMAT') is not None else 'svg'
                    decision_tree = gv.Digraph(format=format, graph_attr={"rankdir": "LR"})

                previous_state_list = []
                action_this_epi = []
                nr = random.random()>0.3 or sqlt.getBestOrder()==None
                acBest = (not nr) and random.random()>0.7

                query_training_start_time = time.time()
                for t in count():
                    action_list, chosen_action, _ = self.dqn.select_action(env, need_random=nr)

                    if self.config["logging"]["use_graphviz"]:
                        for act in action_list:
                            decision_tree.node(str(hash(act)), str(act))

                    logging.debug(f"Chosen action: {chosen_action}")
                    
                    value_now = env.selectValue(self.policy_net)
                    next_value = torch.min(action_list).detach()
                    env_now = copy.deepcopy(env)

                    if acBest:
                        chosen_action = sqlt.getBestOrder()[t]
                    
                    left = chosen_action[0]
                    right = chosen_action[1]
                    env.takeAction(left,right)
                    action_this_epi.append((left,right))

                    if config["logging"]["use_graphviz"]:
                        fn = os.path.basename(sqlt.filename).split('.')[0]
                        decision_tree.render(os.path.join(config["database"]["JOBDir"], fn, f"{fn}_dtree_{t}.gv"))

                    prediction, cost, reward, done = env.reward()
                    reward = torch.tensor([reward], device=self.device, dtype = torch.float32).view(-1,1)

                    previous_state_list.append((value_now,next_value.view(-1,1),env_now))
                    if done:
                        next_value = 0
                        sqlt.updateBestOrder(reward.item(),action_this_epi)

                    expected_state_action_values = (next_value ) + reward.detach()
                    final_state_value = (next_value ) + reward.detach()

                    if done:

                        query_training_end_time = time.time()

                        data = {
                            "episode": i_episode,
                            "query": sqlt.filename,
                            "step": t,
                            "reward": reward.item(),
                            "cost": cost,
                            "base_cost": sqlt.getDPlatency(),
                            "train_duration_ms": (query_training_end_time - query_training_start_time)*1e3
                        }

                        if training_summary is None:
                            training_summary = pd.DataFrame(columns=data.keys())

                        training_summary = training_summary.append(pd.DataFrame(data, index=[0]), ignore_index=True)

                        cnt = 0
                        global tree_lstm_memory
                        tree_lstm_memory = {}
                        self.dqn.Memory.push(env,expected_state_action_values,final_state_value)
                        for pair_s_v in previous_state_list[:0:-1]:
                            cnt += 1
                            if expected_state_action_values > pair_s_v[1]:
                                expected_state_action_values = pair_s_v[1]
                            expected_state_action_values = expected_state_action_values
                            self.dqn.Memory.push(pair_s_v[2],expected_state_action_values,final_state_value)
                        loss = 0

                    if done:
                        loss, pre_gd_time, gd_time = self.dqn.optimize_model()
                        # loss = dqn.optimize_model()
                        # loss = dqn.optimize_model()
                        # loss = dqn.optimize_model()
                        losses.append(loss)
                        avg_eps_losses.append(loss)

                        avg_loss = np.mean(losses)
                        
                        #if (i_episode%self.config["model"]["validate_every"]):
                        if qidx == len(trainSet_temp) - 1:
                            logging.debug(np.mean(losses))
                            logging.debug(f'###################### Epoch {i_episode//self.config["model"]["validate_every"]}')
                            
                            training_time = time.time()-startTime

                            infos = {
                                "episode": i_episode, 
                                "training_time": training_time,                
                                "pre_gd_time_ms": pre_gd_time, 
                                "gd_time_ms": gd_time, 
                                "loss": loss,
                                "avg_loss": avg_loss
                            }
                            
                            _, __validation_summary = self.dqn.validate(validateSet, infos=infos)

                            if validation_summary is None:
                                validation_summary = pd.DataFrame(columns=__validation_summary.columns)

                            validation_summary = validation_summary.append(__validation_summary, ignore_index=True)
                            
                            logging.debug(f"time: {training_time}")
                            logging.debug("~~~~~~~~~~~~~~")
                        break

            early_stopping = False
            save_best_model = False
            if self.config["model"]["early_stopping_patience"] > 0 and i_episode > 0:
                fn = os.path.join("models", self.config["model"]["name"], "summary-validation.csv")
                offset = (i_episode-1)*len(validateSet) + int(i_episode == 1)
                last_validation_loss = pd.read_csv(fn, skiprows=offset, names=validation_summary.columns).tail(1)["loss"].item()
                avg_eps_loss = np.mean(avg_eps_losses).item()

                if avg_eps_loss < self.checkpoint["best_loss"] or best_episode is None:
                    self.checkpoint["best_loss"] = avg_eps_loss
                    best_episode = i_episode
                    save_best_model = True
                
                early_stopping = avg_loss > last_validation_loss and (i_episode - best_episode) >= self.config["model"]["early_stopping_patience"]
            
            if i_episode%self.config['model']['save_every']==0:
                # Save training log
                fn = os.path.join("models", self.config["model"]["name"], "summary-training.csv")
                training_summary.to_csv(fn, mode="a", header=(not os.path.exists(fn)), index=False)
                training_summary = None

                # Save validation log
                fn = os.path.join("models", self.config["model"]["name"], "summary-validation.csv")
                validation_summary.to_csv(fn, mode="a", header=(not os.path.exists(fn)), index=False)
                validation_summary = None

                # Save the model
                torch.save(self.policy_net.state_dict(), os.path.join("models", self.config["model"]["name"], f'CostTraining.pth'))
                self.checkpoint["checkpoint"] = i_episode
                self.checkpoint["latest_model"] = os.path.join("models", self.config["model"]["name"], f'CostTraining.pth')
                self.checkpoint["best_model"] = best_episode
                yaml.dump(self.checkpoint, open(os.path.join("models", self.config["model"]["name"], self.config["model"]["checkpoint"]), 'w'))

                if save_best_model:
                    print("Saving best model...")
                    save_dir = os.path.join('models', config['model']['name'])
                    cmd = f"tar -zcvf {save_dir + '/best-model.tar.gz'} -C {save_dir} {self.config['model']['checkpoint']} CostTraining.pth summary-training.csv summary-validation.csv"
                    subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
                    save_best_model = False

                if early_stopping:
                    print("Stopping early...")
                    break

            if i_episode % self.config['model']['update_target_every'] == 0:
                self.target_net.load_state_dict(self.policy_net.state_dict())
            
        torch.save(self.policy_net.state_dict(), os.path.join("models", self.config["model"]["name"], 'CostTraining.pth'))
        # policy_net = policy_net.cuda()

    def predict(self, queryfiles: List[AnyStr], forceLatency=False) -> str:

        for queryfile in tqdm(queryfiles):
            logging.debug(f"Processing {queryfile}...")
            sqlt = Query(self.runner, open(queryfile, "r").read(), queryfile)

            sql_out = os.path.join("models", self.config["model"]["name"], "prediction", os.path.basename(sqlt.filename))
            os.makedirs(os.path.dirname(sql_out), exist_ok=True)

            if os.path.exists(sql_out):
                continue

            env = ENV(sqlt,self.db_info,self.runner,self.device, self.config)

            previous_state_list = []
            action_this_epi = []
            nr = True
            nr = random.random()>0.3 or sqlt.getBestOrder()==None
            acBest = (not nr) and random.random()>0.7

            query_prediction_start_time = time.time()
            for t in count():
                action_list, chosen_action, all_action = self.dqn.select_action(env, need_random=nr)

                logging.debug(f"Chosen action: {chosen_action}")
                value_now = env.selectValue(self.policy_net)
                next_value = torch.min(action_list).detach()
                env_now = copy.deepcopy(env)

                if acBest:
                    chosen_action = sqlt.getBestOrder()[t]
                left = chosen_action[0]
                right = chosen_action[1]
                env.takeAction(left,right)
                action_this_epi.append((left,right))

                prediction, cost, reward, done = env.reward()
                reward = torch.tensor([reward], device=self.device, dtype = torch.float32).view(-1,1)

                previous_state_list.append((value_now,next_value.view(-1,1),env_now))
                if done:

                    #             logging.debug("done")
                    next_value = 0
                    sqlt.updateBestOrder(reward.item(),action_this_epi)

                expected_state_action_values = (next_value ) + reward.detach()
                final_state_value = (next_value ) + reward.detach()

                if done:
                    cnt = 0
                    global tree_lstm_memory
                    tree_lstm_memory = {}
                    self.dqn.Memory.push(env,expected_state_action_values,final_state_value)
                    for pair_s_v in previous_state_list[:0:-1]:
                        cnt += 1
                        if expected_state_action_values > pair_s_v[1]:
                            expected_state_action_values = pair_s_v[1]
                        expected_state_action_values = expected_state_action_values
                        self.dqn.Memory.push(pair_s_v[2],expected_state_action_values,final_state_value)
                    loss = 0

                if done:
                    query_prediction_end_time = time.time()
                    loss, pre_gd_time, gd_time = self.dqn.optimize_model()                        
                    infos = {
                        "pre_gd_time_ms": pre_gd_time, 
                        "gd_time_ms": gd_time, 
                        "loss": loss,
                        "prediction_duration_ms": (query_prediction_end_time - query_prediction_start_time)*1e3
                    }

                    prediction, summary_prediction = self.dqn.validate([sqlt], forceLatency=forceLatency, infos=infos)

                    with open(sql_out, mode="w") as f:
                        f.write(prediction)
                        f.close()

                    fn = os.path.join("models", self.config["model"]["name"], "summary-prediction.csv")
                    summary_prediction.to_csv(fn, mode="a", header=(not os.path.exists(fn)), index=False)

                    break

if __name__=='__main__':
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', type=str, default="train", help='train | predict')
    parser.add_argument('--queryfile', type=str, default="", nargs="*", help="Relative path to queryfile")
    parser.add_argument('--from-scratch', default=False, action='store_true', help='Whether or not start the training from scratch.')
    parser.add_argument('--force-latency', default=False, action='store_true', help='Only in predict mode: Log the latency instead of the cost.')

    args = parser.parse_args()
    config = yaml.load(open(os.environ["RTOS_CONFIG"], 'r'), Loader=yaml.FullLoader)[os.environ["RTOS_TRAINTYPE"]]

    if args.mode == "train" and args.from_scratch:
        subprocess.Popen(
            f"rm -rf {os.path.join('models', config['model']['name'])}", 
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).communicate()

    subprocess.Popen(
        f"mkdir -p {os.path.join('models', config['model']['name'])}", 
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).communicate()

    ct = CostTraining(config=config)

    if args.mode == "train":
        sytheticQueries = ct.QueryLoader(QueryDir=config["database"]['syntheticDir'])
        # logging.debug(sytheticQueries)
        JOBQueries = ct.QueryLoader(QueryDir=config["database"]['JOBDir'])
        Q4,Q1 = ct.k_fold(JOBQueries,10,1)
        # logging.debug(Q4,Q1)
        ct.train(Q4+sytheticQueries,Q1, n_episodes=config['model']['n_episodes'])
    
    elif args.mode == "predict":
        queryfiles: List[AnyStr] = []
        for q in args.queryfile:
            queryfiles.extend(glob(q))

        print(queryfiles)

        if args.from_scratch:
            subprocess.Popen(
                f"rm -rf {os.path.join('models', config['model']['name'], 'prediction')}", 
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ).communicate()

            subprocess.Popen(
                f"rm -f {os.path.join('models', config['model']['name'], 'summary_*.csv')}", 
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ).communicate()

        ct.predict(queryfiles, forceLatency=args.force_latency)


import argparse
from datetime import datetime
import glob
import logging
import subprocess
from typing import AnyStr, List, Tuple

import yaml
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
import pandas as pd

class Train:
    def __init__(self, rewarder, config: dict) -> None:
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
        
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if config['model']['device'] == "gpu" else torch.device("cpu")
        #device = torch.device("cpu")


        with open(self.config["database"]["pg_schema_file"], "r") as f:
            createSchema = "".join(f.readlines())

        self.db_info = DB(createSchema, config=config)

        self.featureSize = 128

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

        if not os.path.existsos.path.join("models", self.config["model"]["name"], self.config['model']['checkpoint']):
            self.checkpoint = dict({
                "checkpoint": 0,
                "latest_model": os.path.join("models", self.config["model"]["name"], "LatencyTuning.pth")
            })
        else:
            self.checkpoint = yaml.load(open(self.config['model']['checkpoint'], 'r'), Loader=yaml.FullLoader)

        if os.path.exists(self.checkpoint['latest_model']):
            self.policy_net.load_state_dict(torch.load(self.checkpoint["latest_model"], map_location=self.device))
        # policy_net.load_state_dict(torch.load("models/JOB_tc.pth"))#load cost train model
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()

        # policy_net.load_state_dict(torch.load("models/JOB_tc.pth"))#load cost train model
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()


        self.runner = (
            PGRunner(
                config["database"]['pg_dbname'],
                config["database"]['pg_user'],
                config["database"]['pg_password'],
                config["database"]['pg_host'],
                config["database"]['pg_port'],
            ) if config["database"]["engine_class"] == "sql" else
            ISQLRunner(
                config["database"][f'{config["database"]["engine_name"]}_endpoint'],
                config["database"][f'{config["database"]["engine_name"]}_graph'],
                config["database"][f'{config["database"]["engine_name"]}_host'],
                config["database"][f'{config["database"]["engine_name"]}_port'],
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
                    if os.path.splitext(file)[1] == f'.{self.config["database"]["engine_class"]}':
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
            #         sql = val_list[i_episode%len(train_list)]
            pg_cost = sql.getDPlatency()
            #         continue
            env = ENV(sql,self.db_info,self.runner,self.device, self.config)

            for t in count():
                action_list, chosen_action, all_action = self.dqn.select_action(env,need_random=False)

                left = chosen_action[0]
                right = chosen_action[1]
                env.takeAction(left,right)

                prediction, cost, reward, done = env.reward()
                if done:
                    # mrc = max(np.exp(reward*log(1.5))/pg_cost-1,0)
                    # rewardsP.append(np.exp(reward*log(1.5)-log(pg_cost)))
                    # mes += reward*log(1.5)-log(pg_cost)

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

        trainSet_temp = trainSet
        losses = []
        startTime = time.time()
        print_every = self.config['model']['save_every']

        for i_episode in tqdm(range(0,n_episodes), unit="episode"):
            
            if i_episode < self.checkpoint['checkpoint']: continue

            if i_episode % 200 == 100:
                logging.debug("Resampling training set...")
                trainSet = self.resample_sql(trainSet_temp)
            #     sql = random.sample(train_list_back,1)[0][0]
            sqlt = random.sample(trainSet[0:],1)[0]
            env = ENV(sqlt,self.db_info,self.runner,self.device,self.config)
            pg_cost = sqlt.getDPlatency()

            #format = os.environ['RTOS_GV_FORMAT'] if os.environ.get('RTOS_GV_FORMAT') is not None else 'svg'
            #decision_tree = gv.Digraph(format=format, graph_attr={"rankdir": "LR"})

            previous_state_list = []
            action_this_epi = []
            nr = random.random()>0.3 or sqlt.getBestOrder()==None
            acBest = (not nr) and random.random()>0.7
            for t in count():
                # beginTime = time.time();
                action_list, chosen_action, all_action = self.dqn.select_action(env, need_random=nr)

                # for act in action_list:
                #     decision_tree.node(str(hash(act)), str(act))

                logging.debug(f"Action list: {action_list}, chosen action: {chosen_action}")
                value_now = env.selectValue(self.policy_net)
                next_value = torch.min(action_list).detach()
                # e1Time = time.time()
                env_now = copy.deepcopy(env)
                # endTime = time.time()
                # logging.debug(f"make {endTime-startTime,endTime-e1Time}")
                if acBest:
                    chosen_action = sqlt.getBestOrder()[t]
                left = chosen_action[0]
                right = chosen_action[1]
                env.takeAction(left,right)
                action_this_epi.append((left,right))

                # fn = os.path.basename(sqlt.filename).split('.')[0]
                # decision_tree.render(os.path.join(config["database"]["JOBDir"], fn, f"{fn}_dtree_{t}.gv"))

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
                    #             for idx in range(t-cnt+1):
                    global tree_lstm_memory
                    tree_lstm_memory = {}
                    self.dqn.Memory.push(env,expected_state_action_values,final_state_value)
                    for pair_s_v in previous_state_list[:0:-1]:
                        cnt += 1
                        if expected_state_action_values > pair_s_v[1]:
                            expected_state_action_values = pair_s_v[1]
                        #                 for idx in range(cnt):
                        expected_state_action_values = expected_state_action_values
                        self.dqn.Memory.push(pair_s_v[2],expected_state_action_values,final_state_value)
                    #                 break
                    loss = 0

                if done:
                    # break
                    loss, pre_gd_time, gd_time = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()
                    losses.append(loss)
                    if (i_episode%print_every==0):
                        logging.debug(np.mean(losses))
                        logging.debug(f"###################### Epoch {i_episode//print_every}, baseline-cost = {pg_cost}")
                        
                        mrc, gmrl = self.dqn.validate(validateSet)
                        training_time = time.time()-startTime

                        fn = os.path.join(self.config["database"]['JOBDir'], "validation.csv")
                        pd.DataFrame(
                            [[i_episode+1, training_time, mrc, gmrl, pg_cost, pre_gd_time, gd_time]], 
                            columns=["episode", "training_time", "mrc", "gmrl", "pg_cost", "pre_gd_time, gd_time"]
                        ).to_csv(fn, mode="a", header=(not os.path.exists(fn)), index=False)
                        logging.debug(f"time: {training_time}")
                        logging.debug("~~~~~~~~~~~~~~")

                        torch.save(self.policy_net.state_dict(), os.path.join("models", self.config["model"]["name"], f'LatencyTuning_eps{i_episode}.pth'))
                        self.checkpoint["checkpoint"] = i_episode
                        self.checkpoint["latest_model"] = os.path.join("models", self.config["model"]["name"], f'LatencyTuning_eps{i_episode}.pth')
                        yaml.dump(self.checkpoint, open(os.path.join("models", self.config["model"]["name"], self.config["model"]["checkpoint"]), 'w'))


                    break
            if i_episode % self.config['model']['update_target_every'] == 0:
                self.target_net.load_state_dict(self.policy_net.state_dict())
        torch.save(self.policy_net.state_dict(), os.path.join("models", self.config["model"]["name"], 'LatencyTuning.pth'))
        # policy_net = policy_net.cuda()

    def predict(self,queryfiles: List[AnyStr]) -> str:

        for queryfile in queryfiles:
            sqlt = Query(self.runner, open(queryfile, "r").read(), queryfile)
            env = ENV(sqlt,self.db_info,self.runner,self.device, self.config)

            previous_state_list = []
            action_this_epi = []
            nr = True
            nr = random.random()>0.3 or sqlt.getBestOrder()==None
            acBest = (not nr) and random.random()>0.7
            for t in count():
                # beginTime = time.time();
                action_list, chosen_action,all_action = self.dqn.select_action(env,need_random=nr)
                value_now = env.selectValue(self.policy_net)
                next_value = torch.min(action_list).detach()
                # e1Time = time.time()
                env_now = copy.deepcopy(env)
                # endTime = time.time()
                # logging.debug("make",endTime-startTime,endTime-e1Time)
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
                    next_value = 0
                    sqlt.updateBestOrder(reward.item(),action_this_epi)

                expected_state_action_values = (next_value ) + reward.detach()
                final_state_value = (next_value ) + reward.detach()

                if done:
                    cnt = 0
                    #             for idx in range(t-cnt+1):
                    global tree_lstm_memory
                    tree_lstm_memory = {}
                    self.dqn.Memory.push(env,expected_state_action_values,final_state_value)
                    for pair_s_v in previous_state_list[:0:-1]:
                        cnt += 1
                        if expected_state_action_values > pair_s_v[1]:
                            expected_state_action_values = pair_s_v[1]
                        #                 for idx in range(cnt):
                        expected_state_action_values = expected_state_action_values
                        self.dqn.Memory.push(pair_s_v[2],expected_state_action_values,final_state_value)
                    #                 break
                    loss = 0

                if done:
                    # break
                    loss = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()
                    loss = self.dqn.optimize_model()

                    print("================================")
                    print(f"Prediction for { queryfile }:")
                    print(env.getPlan())
                    print(f"Reward: {reward}")
                    print("================================")
                    break

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', type=str, default="train", help='train | predict')
    parser.add_argument('--queryfile', type=str, default="", nargs="*", help="Relative path to queryfile")
    parser.add_argument('--restart', type=bool, default=False, help='Whether or not start the training from scratch.')
    args = parser.parse_args()

    config = yaml.load(open(os.environ["RTOS_CONFIG"], 'r'), Loader=yaml.FullLoader)[os.environ["RTOS_TRAINTYPE"]]

    if args.restart:
        subprocess.Popen(
            f"rm -rf {os.path.join('models', config['model']['name'])}", 
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).communicate()

    subprocess.Popen(
        f"mkdir -p {os.path.join('models', config['model']['name'])}", 
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).communicate()

    lt = Train(config=config)

    if args.mode == "train":
        sytheticQueries = lt.QueryLoader(QueryDir=config["database"]['syntheticDir'])
        # logging.debug(sytheticQueries)
        JOBQueries = lt.QueryLoader(QueryDir=config["database"]['JOBDir'])
        Q4,Q1 = lt.k_fold(JOBQueries,10,1)
        # logging.debug(Q4,Q1)
        lt.train(Q4+sytheticQueries,Q1, n_episodes=config['model']['n_episodes'])
    elif args.mode == "predict":
        queryfiles: List[AnyStr] = []
        for q in args.queryfile:
            queryfiles.extend(glob(q))

        print(queryfiles)

        lt.predict(queryfiles)

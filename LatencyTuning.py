
from utils.DBUtils import PGRunner, ISQLRunner
from utils.sqlSample import sqlInfo
import numpy as np
from itertools import count
from math import log
import random
import time
from DQN import DQN,ENV
from utils.TreeLSTM import SPINN
from utils.JOBParser import DB
import copy
import torch
from torch.nn import init
from ImportantConfig import Config

import os
import pandas as pd

config = Config()

device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if os.environ['RTOS_PYTORCH_DEVICE'] == "gpu" else torch.device("cpu")
#device = torch.device("cpu")

with open(config.schemaFile, "r") as f:
    createSchema = "".join(f.readlines())

db_info = DB(createSchema)

featureSize = 128

policy_net = SPINN(
    n_classes = 1, size = featureSize, 
    n_words = config.n_words,
    mask_size= len(db_info)*len(db_info),
    device=device, 
    max_column_in_table=config.max_column_in_table
).to(device)

target_net = SPINN(
    n_classes = 1, size = featureSize, 
    n_words = config.n_words, 
    mask_size= len(db_info)*len(db_info),
    device=device, 
    max_column_in_table=config.max_column_in_table
).to(device)

policy_net.load_state_dict(torch.load("models/CostTraining.pth"))
target_net.load_state_dict(policy_net.state_dict())
target_net.eval()

runner = (
    PGRunner(
        config.sql_dbName,
        config.sql_userName,
        config.sql_password,
        config.sql_ip,
        config.sql_port,
        isCostTraining=False,
        latencyRecord = True,
        latencyRecordFile = "Latency.json"
    ) if os.environ["RTOS_ENGINE"] == "sql" else
    ISQLRunner(
        config.isql_endpoint,
        config.isql_graph,
        config.isql_host,
        config.isql_port,
        isCostTraining=False,
        latencyRecord = True,
        latencyRecordFile = "Latency.json"
    )
)

dqn = DQN(policy_net,target_net,db_info,runner,device)

def k_fold(input_list,k,ix = 0):
    li = len(input_list)
    kl = (li-1)//k + 1
    train = []
    validate = []
    for idx in range(li):

        if idx%k == ix:
            validate.append(input_list[idx])
        else:
            train.append(input_list[idx])
    return train,validate


def QueryLoader(QueryDir):
    def file_name(file_dir):
        import os
        L = []
        for root, dirs, files in os.walk(file_dir):
            for file in files:
                if os.path.splitext(file)[1] in ['.sql', '.sparql']:
                    L.append(os.path.join(root, file))
        return L
    files = file_name(QueryDir)
    sql_list = []
    for filename in files:
        with open(filename, "r") as f:
            data = f.readlines()
            one_sql = "".join(data)
            sql_list.append(sqlInfo(runner,one_sql,filename))
    return sql_list

def resample_sql(sql_list):
    rewards = []
    reward_sum = 0
    rewardsP = []
    mes = 0
    for sql in sql_list:
        #         sql = val_list[i_episode%len(train_list)]
        pg_cost = sql.getDPlatency()
        #         continue
        env = ENV(sql,db_info,runner,device)

        for t in count():
            action_list, chosen_action,all_action = dqn.select_action(env,need_random=False)

            left = chosen_action[0]
            right = chosen_action[1]
            env.takeAction(left,right)

            reward, done = env.reward()
            if done:
                # mrc = max(np.exp(reward*log(1.5))/pg_cost-1,0)
                # rewardsP.append(np.exp(reward*log(1.5)-log(pg_cost)))
                # mes += reward*log(1.5)-log(pg_cost)

                mrc = max(reward/pg_cost-1, 0)
                rewardsP.append(np.exp(log(reward)-log(pg_cost)))
                mes += log(reward)-log(pg_cost)
                rewards.append((mrc,sql))
                reward_sum += mrc
                break

    import random
    print(rewardsP)
    res_sql = []
    print(mes/len(sql_list))
    for idx in range(len(sql_list)):
        rd = random.random()*reward_sum
        for ts in range(len(sql_list)):
            rd -= rewards[ts][0]
            if rd<0:
                res_sql.append(rewards[ts][1])
                break
    return res_sql+sql_list

def predict():
    sqlt = random.sample(trainSet[0:],1)[0]
    pg_cost = sqlt.getDPlatency()
    env = ENV(sqlt,db_info,runner,device)

    previous_state_list = []
    action_this_epi = []
    nr = True
    nr = random.random()>0.3 or sqlt.getBestOrder()==None
    acBest = (not nr) and random.random()>0.7
    for t in count():
        # beginTime = time.time();
        action_list, chosen_action,all_action = dqn.select_action(env,need_random=nr)
        value_now = env.selectValue(policy_net)
        next_value = torch.min(action_list).detach()
        # e1Time = time.time()
        env_now = copy.deepcopy(env)
        # endTime = time.time()
        # print("make",endTime-startTime,endTime-e1Time)
        if acBest:
            chosen_action = sqlt.getBestOrder()[t]
        left = chosen_action[0]
        right = chosen_action[1]
        env.takeAction(left,right)
        action_this_epi.append((left,right))

        reward, done = env.reward()
        reward = torch.tensor([reward], device=device, dtype = torch.float32).view(-1,1)

        previous_state_list.append((value_now,next_value.view(-1,1),env_now))
        if done:

            #             print("done")
            next_value = 0
            sqlt.updateBestOrder(reward.item(),action_this_epi)

        expected_state_action_values = (next_value ) + reward.detach()
        final_state_value = (next_value ) + reward.detach()

        if done:
            cnt = 0
            #             for idx in range(t-cnt+1):
            global tree_lstm_memory
            tree_lstm_memory = {}
            dqn.Memory.push(env,expected_state_action_values,final_state_value)
            for pair_s_v in previous_state_list[:0:-1]:
                cnt += 1
                if expected_state_action_values > pair_s_v[1]:
                    expected_state_action_values = pair_s_v[1]
                #                 for idx in range(cnt):
                expected_state_action_values = expected_state_action_values
                dqn.Memory.push(pair_s_v[2],expected_state_action_values,final_state_value)
            #                 break
            loss = 0

        if done:
            plan = env.getPlan()
            break

def train(trainSet,validateSet):

    trainSet_temp = trainSet
    losses = []
    startTime = time.time()
    print_every = 20
    TARGET_UPDATE = 3
    for i_episode in range(0,10000):
        if i_episode % 200 == 100:
            trainSet = resample_sql(trainSet_temp)
        #     sql = random.sample(train_list_back,1)[0][0]
        sqlt = random.sample(trainSet[0:],1)[0]
        pg_cost = sqlt.getDPlatency()
        env = ENV(sqlt,db_info,runner,device)

        previous_state_list = []
        action_this_epi = []
        nr = True
        nr = random.random()>0.3 or sqlt.getBestOrder()==None
        acBest = (not nr) and random.random()>0.7
        for t in count():
            # beginTime = time.time();
            action_list, chosen_action,all_action = dqn.select_action(env,need_random=nr)
            value_now = env.selectValue(policy_net)
            next_value = torch.min(action_list).detach()
            # e1Time = time.time()
            env_now = copy.deepcopy(env)
            # endTime = time.time()
            # print("make",endTime-startTime,endTime-e1Time)
            if acBest:
                chosen_action = sqlt.getBestOrder()[t]
            left = chosen_action[0]
            right = chosen_action[1]
            env.takeAction(left,right)
            action_this_epi.append((left,right))

            reward, done = env.reward()
            reward = torch.tensor([reward], device=device, dtype = torch.float32).view(-1,1)

            previous_state_list.append((value_now,next_value.view(-1,1),env_now))
            if done:

                #             print("done")
                next_value = 0
                sqlt.updateBestOrder(reward.item(),action_this_epi)

            expected_state_action_values = (next_value ) + reward.detach()
            final_state_value = (next_value ) + reward.detach()

            if done:
                cnt = 0
                #             for idx in range(t-cnt+1):
                global tree_lstm_memory
                tree_lstm_memory = {}
                dqn.Memory.push(env,expected_state_action_values,final_state_value)
                for pair_s_v in previous_state_list[:0:-1]:
                    cnt += 1
                    if expected_state_action_values > pair_s_v[1]:
                        expected_state_action_values = pair_s_v[1]
                    #                 for idx in range(cnt):
                    expected_state_action_values = expected_state_action_values
                    dqn.Memory.push(pair_s_v[2],expected_state_action_values,final_state_value)
                #                 break
                loss = 0

            if done:
                # break
                loss = dqn.optimize_model()
                loss = dqn.optimize_model()
                loss = dqn.optimize_model()
                loss = dqn.optimize_model()
                losses.append(loss)
                if ((i_episode + 1)%print_every==0):
                    print(np.mean(losses))
                    print("###################### Epoch",i_episode//print_every,pg_cost)

                    mrc, gmrl = dqn.validate(validateSet)
                    training_time = time.time()-startTime

                    fn = os.path.join(Config().JOBDir, "validation.csv")
                    pd.DataFrame(
                        [[i_episode+1, training_time, mrc, gmrl, pg_cost]], 
                        columns=["episode", "training_time", "mrc", "gmrl", "pg_cost"]
                    ).to_csv(fn, mode="a", header=(not os.path.exists(fn)), index=False)
                    print("time", training_time)
                    print("~~~~~~~~~~~~~~")
                break
        if i_episode % TARGET_UPDATE == 0:
            target_net.load_state_dict(policy_net.state_dict())
    torch.save(policy_net.cpu().state_dict(), 'models/LatencyTuning.pth')

if __name__=='__main__':
    sytheticQueries = QueryLoader(QueryDir=config.sytheticDir)
    # print(sytheticQueries)
    JOBQueries = QueryLoader(QueryDir=config.JOBDir)
    Q4,Q1 = k_fold(JOBQueries,10,1)
    # print(Q4,Q1)
    train(Q4+sytheticQueries,Q1)

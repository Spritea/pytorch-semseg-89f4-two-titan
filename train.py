import os
import sys
import yaml
import time
import shutil
import torch
import random
import argparse
import datetime
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models

from torch.utils import data
from tqdm import tqdm

from ptsemseg.models import get_model
from ptsemseg.loss import get_loss_function
from ptsemseg.loader import get_loader 
from ptsemseg.utils import get_logger
from ptsemseg.metrics import runningScore, averageMeter
from ptsemseg.augmentations import get_composed_augmentations
from ptsemseg.schedulers import get_scheduler
from ptsemseg.optimizers import get_optimizer

from tensorboardX import SummaryWriter

# from apex.fp16_utils import FP16_Optimizer
import my_pt
# import apex
# import encoding

def train(cfg, writer, logger,run_id):
    
    # Setup seeds
    torch.manual_seed(cfg.get('seed', 1337))
    torch.cuda.manual_seed(cfg.get('seed', 1337))
    np.random.seed(cfg.get('seed', 1337))
    random.seed(cfg.get('seed', 1337))

    # torch.backends.cudnn.deterministic = True
    # torch.backends.cudnn.benchmark = False

    torch.backends.cudnn.benchmark=True

    os.environ["CUDA_VISIBLE_DEVICES"] = "0,1"
    # Setup device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Setup Augmentations
    augmentations = cfg['training'].get('augmentations', None)
    data_aug = get_composed_augmentations(augmentations)

    # Setup Dataloader
    data_loader = get_loader(cfg['data']['dataset'])
    data_path = cfg['data']['path']

    logger.info("Using dataset: {}".format(data_path))


    t_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg['data']['train_split'],
        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),
        augmentations=data_aug)

    v_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg['data']['val_split'],
        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),)

    n_classes = t_loader.n_classes
    trainloader = data.DataLoader(t_loader,
                                  batch_size=cfg['training']['batch_size'], 
                                  num_workers=cfg['training']['n_workers'], 
                                  shuffle=True)

    valloader = data.DataLoader(v_loader, 
                                batch_size=cfg['training']['batch_size'], 
                                num_workers=cfg['training']['n_workers'])

    # Setup Metrics
    running_metrics_val = runningScore(n_classes)

    # Setup Model
    # model = get_model(cfg['model'], n_classes).to(device)
    model = get_model(cfg['model'], n_classes)
    logger.info("Using Model: {}".format(cfg['model']['arch']))

    # model=apex.parallel.convert_syncbn_model(model)
    model=model.to(device)


    # a=range(torch.cuda.device_count())
    # model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
    model = torch.nn.DataParallel(model, device_ids=[0,1])
    # model = encoding.parallel.DataParallelModel(model, device_ids=[0, 1])

    # Setup optimizer, lr_scheduler and loss function
    optimizer_cls = get_optimizer(cfg)
    optimizer_params = {k:v for k, v in cfg['training']['optimizer'].items() 
                        if k != 'name'}

    optimizer = optimizer_cls(model.parameters(), **optimizer_params)

    # optimizer = FP16_Optimizer(optimizer, static_loss_scale=128.0)

    logger.info("Using optimizer {}".format(optimizer))

    scheduler = get_scheduler(optimizer, cfg['training']['lr_schedule'])

    # optimizer = FP16_Optimizer(optimizer, static_loss_scale=128.0)


    loss_fn = get_loss_function(cfg)
    # loss_fn== encoding.parallel.DataParallelCriterion(loss_fn, device_ids=[0, 1])
    logger.info("Using loss {}".format(loss_fn))

    start_iter = 0
    if cfg['training']['resume'] is not None:
        if os.path.isfile(cfg['training']['resume']):
            logger.info(
                "Loading model and optimizer from checkpoint '{}'".format(cfg['training']['resume'])
            )
            checkpoint = torch.load(cfg['training']['resume'])
            model.load_state_dict(checkpoint["model_state"])
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            # start_iter = checkpoint["epoch"]
            logger.info(
                "Loaded checkpoint '{}' (iter {})".format(
                    cfg['training']['resume'], checkpoint["epoch"]
                )
            )
        else:
            logger.info("No checkpoint found at '{}'".format(cfg['training']['resume']))

    val_loss_meter = averageMeter()
    time_meter = averageMeter()
    time_meter_val=averageMeter()

    best_iou = -100.0
    i = start_iter
    flag = True

    train_data_len = t_loader.__len__()
    batch_size = cfg['training']['batch_size']
    epoch = cfg['training']['train_epoch']
    train_iter = int(np.ceil(train_data_len / batch_size) * epoch)

    val_rlt_f1=[]
    val_rlt_OA=[]
    best_f1_till_now=0
    best_OA_till_now=0

    while i <= train_iter and flag:
        for (images, labels) in trainloader:
            i += 1
            start_ts = time.time()
            scheduler.step()
            model.train()
            images = images.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            outputs = model(images)

            loss = loss_fn(input=outputs, target=labels)

            loss.backward()
            # optimizer.backward(loss)

            optimizer.step()
            
            time_meter.update(time.time() - start_ts)

            ### add by Sprit
            time_meter_val.update(time.time() - start_ts)


            if (i + 1) % cfg['training']['print_interval'] == 0:
                fmt_str = "Iter [{:d}/{:d}]  Loss: {:.4f}  Time/Image: {:.4f}"
                print_str = fmt_str.format(i + 1,
                                           train_iter,
                                           loss.item(),
                                           time_meter.avg / cfg['training']['batch_size'])


                print(print_str)
                logger.info(print_str)
                writer.add_scalar('loss/train_loss', loss.item(), i+1)
                time_meter.reset()

            if (i + 1) % cfg['training']['val_interval'] == 0 or \
               (i + 1) == train_iter:
                model.eval()
                with torch.no_grad():
                    for i_val, (images_val, labels_val) in tqdm(enumerate(valloader)):
                        images_val = images_val.to(device)
                        labels_val = labels_val.to(device)

                        outputs = model(images_val)
                        # val_loss = loss_fn(input=outputs, target=labels_val)

                        pred = outputs.data.max(1)[1].cpu().numpy()
                        gt = labels_val.data.cpu().numpy()


                        running_metrics_val.update(gt, pred)
                        # val_loss_meter.update(val_loss.item())

                # writer.add_scalar('loss/val_loss', val_loss_meter.avg, i+1)
                # logger.info("Iter %d Loss: %.4f" % (i + 1, val_loss_meter.avg))

                score, class_iou = running_metrics_val.get_scores()

                for k, v in score.items():
                    print(k, v)
                    logger.info('{}: {}'.format(k, v))
                    # writer.add_scalar('val_metrics/{}'.format(k), v, i+1)

                for k, v in class_iou.items():
                    logger.info('{}: {}'.format(k, v))
                    # writer.add_scalar('val_metrics/cls_{}'.format(k), v, i+1)

                # val_loss_meter.reset()
                running_metrics_val.reset()

                ### add by Sprit
                avg_f1 = score["Mean F1 : \t"]
                OA=score["Overall Acc: \t"]
                val_rlt_f1.append(avg_f1)
                val_rlt_OA.append(score["Overall Acc: \t"])

                if avg_f1 >= best_f1_till_now:
                    best_f1_till_now = avg_f1
                    correspond_OA = score["Overall Acc: \t"]
                    best_f1_epoch_till_now = i+1
                print("\nBest F1 till now = ", best_f1_till_now)
                print("Correspond OA= ", correspond_OA)
                print("Best F1 Iter till now= ", best_f1_epoch_till_now)

                if OA >= best_OA_till_now:
                    best_OA_till_now = OA
                    correspond_f1 = score["Mean F1 : \t"]
                    # correspond_acc=score["Overall Acc: \t"]
                    best_OA_epoch_till_now = i+1

                    state = {
                        "epoch": i + 1,
                        "model_state": model.state_dict(),
                        "optimizer_state": optimizer.state_dict(),
                        "scheduler_state": scheduler.state_dict(),
                        "best_OA": best_OA_till_now,
                    }
                    save_path = os.path.join(writer.file_writer.get_logdir(),
                                             "{}_{}_best_model.pkl".format(
                                                 cfg['model']['arch'],
                                                 cfg['data']['dataset']))
                    torch.save(state, save_path)

                print("Best OA till now = ", best_OA_till_now)
                print("Correspond F1= ", correspond_f1)
                # print("Correspond OA= ",correspond_acc)
                print("Best OA Iter till now= ", best_OA_epoch_till_now)

                ### add by Sprit
                iter_time=time_meter_val.avg
                time_meter_val.reset()
                remain_time = iter_time * (train_iter - i)
                m, s = divmod(remain_time, 60)
                h, m = divmod(m, 60)
                if s != 0:
                    train_time = "Remain training time = %d hours %d minutes %d seconds \n" % (h, m, s)
                else:
                    train_time = "Remain training time : Training completed.\n"
                print(train_time)

                # if OA >= best_OA_till_now:
                #     best_iou = score["Mean IoU : \t"]
                #     state = {
                #         "epoch": i + 1,
                #         "model_state": model.state_dict(),
                #         "optimizer_state": optimizer.state_dict(),
                #         "scheduler_state": scheduler.state_dict(),
                #         "best_iou": best_iou,
                #     }
                #     save_path = os.path.join(writer.file_writer.get_logdir(),
                #                              "{}_{}_best_model.pkl".format(
                #                                  cfg['model']['arch'],
                #                                  cfg['data']['dataset']))
                #     torch.save(state, save_path)

            if (i + 1) == train_iter:
                flag = False
                break
    my_pt.csv_out(run_id,data_path,cfg['model']['arch'],epoch,val_rlt_f1,cfg['training']['val_interval'])
    my_pt.csv_out(run_id,data_path,cfg['model']['arch'],epoch,val_rlt_OA,cfg['training']['val_interval'])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument(
        "--config",
        nargs="?",
        type=str,
        default="configs/fcn8s_pascal.yml",
        help="Configuration file to use"
    )

    args = parser.parse_args()

    with open(args.config) as fp:
        cfg = yaml.load(fp)

    run_id = random.randint(1,100000)
    logdir = os.path.join('runs', os.path.basename(args.config)[:-4] , str(run_id))
    writer = SummaryWriter(log_dir=logdir)

    print('RUNDIR: {}'.format(logdir))
    shutil.copy(args.config, logdir)

    logger = get_logger(logdir)
    logger.info('Let the games begin')

    train(cfg, writer, logger,run_id)

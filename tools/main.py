import argparse
import os.path as osp
import sys
import time

import torch
from scripts.default_config import (engine_run_kwargs, get_default_config,
                                    imagedata_kwargs, lr_finder_run_kwargs,
                                    lr_scheduler_kwargs, model_kwargs,
                                    optimizer_kwargs, videodata_kwargs)
from scripts.script_utils import build_base_argparser, reset_config, check_classes_consistency

import torchreid
import numpy as np
from torchreid.engine import build_engine
from torchreid.ops import DataParallel
from torchreid.utils import (Logger, check_isfile, collect_env_info,
                             compute_model_complexity,resume_from_checkpoint,
                             set_random_seed, load_pretrained_weights)


def build_datamanager(cfg, classification_classes_filter=None):
    if cfg.data.type == 'image':
        return torchreid.data.ImageDataManager(filter_classes=classification_classes_filter, **imagedata_kwargs(cfg))
    else:
        return torchreid.data.VideoDataManager(**videodata_kwargs(cfg))

def build_auxiliary_model(config_file, num_classes, use_gpu, device_ids=None, lr=None):
    def compute_s(num_class: int):
        return float(max(np.sqrt(2) * np.log(num_class - 1), 3))

    aux_cfg = get_default_config()
    aux_cfg.use_gpu = use_gpu
    aux_cfg.merge_from_file(config_file)

    model = torchreid.models.build_model(**model_kwargs(aux_cfg, num_classes))

    if aux_cfg.use_gpu:
        assert device_ids is not None

        model = DataParallel(model, device_ids=device_ids, output_device=0).cuda(device_ids[0])
    if lr is not None:
        aux_cfg.train.lr = lr
        print(f"setting learning rate from main model, estimated by lr finder: {lr}")
    if aux_cfg.loss.name == 'am_softmax':
        s = compute_s(num_classes[0])
        print(f"computed margin scale for dataset: {s}")
        aux_cfg.loss.softmax.s = s
    optimizer = torchreid.optim.build_optimizer(model, **optimizer_kwargs(aux_cfg))
    scheduler = torchreid.optim.build_lr_scheduler(optimizer, **lr_scheduler_kwargs(aux_cfg))

    if aux_cfg.model.resume.snapshot and check_isfile(aux_cfg.model.resume.snapshot):
        aux_cfg.train.start_epoch = resume_from_checkpoint(
            aux_cfg.model.resume.snapshot, model, optimizer=optimizer, scheduler=scheduler, only_weights=aux_cfg.model.resume.weights_only
        )

    return model, optimizer, scheduler

def main():
    parser = build_base_argparser()
    parser.add_argument('-e', '--auxiliary-models-cfg', type=str, nargs='*', default='',
                        help='path to extra config files')
    parser.add_argument('--split-models', action='store_true',
                        help='whether to split models on own gpu')
    args = parser.parse_args()

    cfg = get_default_config()
    cfg.use_gpu = torch.cuda.is_available() and args.gpu_num > 0
    if args.config_file:
        cfg.merge_from_file(args.config_file)
    reset_config(cfg, args)
    cfg.merge_from_list(args.opts)
    set_random_seed(cfg.train.seed)

    log_name = 'test.log' if cfg.test.evaluate else 'train.log'
    log_name += time.strftime('-%Y-%m-%d-%H-%M-%S')
    sys.stdout = Logger(osp.join(cfg.data.save_dir, log_name))

    print('Show configuration\n{}\n'.format(cfg))
    print('Collecting env info ...')
    print('** System info **\n{}\n'.format(collect_env_info()))

    if cfg.use_gpu:
        torch.backends.cudnn.benchmark = True

    enable_mutual_learning = len(cfg.mutual_learning.aux_configs) > 0

    datamanager = build_datamanager(cfg, args.classes)
    num_train_classes = datamanager.num_train_pids

    print('Building main model: {}'.format(cfg.model.name))
    model = torchreid.models.build_model(**model_kwargs(cfg, num_train_classes))
    num_params, flops = compute_model_complexity(model, (1, 3, cfg.data.height, cfg.data.width))
    print('Main model complexity: params={:,} flops={:,}'.format(num_params, flops))

    if cfg.model.load_weights and check_isfile(cfg.model.load_weights):
        load_pretrained_weights(model, cfg.model.load_weights)

    if cfg.model.classification:
        classes_map = {v : k for k, v in enumerate(sorted(args.classes))} if args.classes else {}
        if cfg.test.evaluate:
            for name, dataloader in datamanager.test_loader.items():
                if not len(dataloader['query'].dataset.classes): # current text annotation doesn't contain classes names
                    print(f'Warning: classes are not defined for validation dataset {name}')
                    continue
                if not len(model.classification_classes):
                    print(f'Warning: classes are not provided in the current snapshot. Consistency checks are skipped.')
                    continue
                if not check_classes_consistency(model.classification_classes,
                                                 dataloader['query'].dataset.classes, strict=False):
                    raise ValueError('Inconsistent classes in evaluation dataset')
                if args.classes and not check_classes_consistency(classes_map,
                                                                  model.classification_classes, strict=True):
                    raise ValueError('Classes provided via --classes should be the same as in the loaded model')
        elif args.classes:
            if not check_classes_consistency(classes_map,
                                             datamanager.train_loader.dataset.classes, strict=True):
                raise ValueError('Inconsistent classes in training dataset')

    if cfg.use_gpu:
        num_devices = min(torch.cuda.device_count(), args.gpu_num)
        if enable_mutual_learning and args.split_models:
            num_models = len(cfg.mutual_learning.aux_configs) + 1
            assert num_devices >= num_models
            assert num_devices % num_models == 0

            num_devices_per_model = num_devices // num_models
            device_splits = []
            for model_id in range(num_models):
                device_splits.append([
                    model_id * num_devices_per_model + i
                    for i in range(num_devices_per_model)
                ])

            main_device_ids = device_splits[0]
            extra_device_ids = device_splits[1:]
        else:
            main_device_ids = list(range(num_devices))
            extra_device_ids = [main_device_ids for _ in range(len(cfg.mutual_learning.aux_configs))]

        model = DataParallel(model, device_ids=main_device_ids, output_device=0).cuda(main_device_ids[0])
    else:
        extra_device_ids = [None for _ in range(len(cfg.mutual_learning.aux_configs))]

    optimizer = torchreid.optim.build_optimizer(model, **optimizer_kwargs(cfg))

    if cfg.lr_finder.enable and cfg.lr_finder.mode == 'automatic' and not cfg.model.resume.snapshot:
        scheduler = None
    else:
        scheduler = torchreid.optim.build_lr_scheduler(optimizer, **lr_scheduler_kwargs(cfg))

    if cfg.model.resume.snapshot and check_isfile(cfg.model.resume.snapshot):
        cfg.train.start_epoch = resume_from_checkpoint(
            cfg.model.resume.snapshot, model, optimizer=optimizer, scheduler=scheduler, cfg.model.resume.weights_only
        )

    lr = None # placeholder, needed for aux models
    if cfg.lr_finder.enable and not cfg.test.evaluate and not cfg.model.resume.snapshot:
        if enable_mutual_learning:
            print("Mutual learning is enabled. Learning rate will be estimated for the main model only.")

        # build new engine
        engine = build_engine(cfg, datamanager, model, optimizer, scheduler)
        lr = engine.find_lr(**lr_finder_run_kwargs(cfg))

        print(f"Estimated learning rate: {lr}")
        if cfg.lr_finder.stop_after:
            print("Finding learning rate finished. Terminate the training process")
            exit()

        # reload random seeds, opimizer with new lr and scheduler for it
        cfg.train.lr = lr
        cfg.lr_finder.enable = False
        set_random_seed(cfg.train.seed)

        optimizer = torchreid.optim.build_optimizer(model, **optimizer_kwargs(cfg))
        scheduler = torchreid.optim.build_lr_scheduler(optimizer, **lr_scheduler_kwargs(cfg))

    if enable_mutual_learning:
        print('Enabled mutual learning between {} models.'.format(len(cfg.mutual_learning.aux_configs) + 1))

        models, optimizers, schedulers = [model], [optimizer], [scheduler]
        for config_file, device_ids in zip(cfg.mutual_learning.aux_configs, extra_device_ids):
            aux_model, aux_optimizer, aux_scheduler = build_auxiliary_model(
                config_file, num_train_classes, cfg.use_gpu, device_ids, lr=lr
            )

            models.append(aux_model)
            optimizers.append(aux_optimizer)
            schedulers.append(aux_scheduler)
    else:
        models, optimizers, schedulers = model, optimizer, scheduler

    print('Building {}-engine for {}-reid'.format(cfg.loss.name, cfg.data.type))
    engine = build_engine(cfg, datamanager, models, optimizers, schedulers)

    engine.run(**engine_run_kwargs(cfg))


if __name__ == '__main__':
    main()

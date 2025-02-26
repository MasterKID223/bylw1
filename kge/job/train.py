from datetime import datetime
import itertools
import os
import math
import time
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import partial

import dgl
import torch
import torch.utils.data
import numpy as np
from torch.utils.data import DataLoader
from tqdm import tqdm

from kge import Config, Dataset
from kge.job import Job
from kge.model import KgeModel
from kge.model.evokg_model import data, utils, settings
from kge.model.evokg_model.evokg.model import EmbeddingUpdater, Combiner, EdgeModel, InterEventTimeModel, Model, \
    MultiAspectEmbedding
from kge.model.evokg_model.evokg.time_interval_transform import TimeIntervalTransform
from kge.model.evokg_model.train import compute_loss
from kge.model.evokg_model.utils.log_utils import get_log_root_path
from kge.model.evokg_model.utils.model_utils import get_embedding
from kge.model.evokg_model.utils.train_utils import activation_string


from kge.util import KgeLoss, KgeOptimizer, KgeSampler, KgeLRScheduler
from typing import Any, Callable, Dict, List, Optional, Union
import kge.job.util

SLOTS = [0, 1, 2]
S, P, O = SLOTS


def _generate_worker_init_fn(config):
    "Initialize workers of a DataLoader"
    use_fixed_seed = config.get("random_seed.numpy") >= 0

    def worker_init_fn(worker_num):
        # ensure that NumPy uses different seeds at each worker
        if use_fixed_seed:
            # reseed based on current seed (same for all workers) and worker number
            # (different)
            base_seed = np.random.randint(2 ** 32 - 1)
            np.random.seed(base_seed + worker_num)
        else:
            # reseed fresh
            np.random.seed()

    return worker_init_fn


class TrainingJob(Job):
    """Abstract base job to train a single model with a fixed set of hyperparameters.

    Also used by jobs such as :class:`SearchJob`.

    Subclasses for specific training methods need to implement `_prepare` and
    `_process_batch`.

    """

    def __init__(
            self, config: Config, dataset: Dataset, parent_job: Job = None, model=None
    ) -> None:
        from kge.job import EvaluationJob

        super().__init__(config, dataset, parent_job)
        if model is None:
            self.model: KgeModel = KgeModel.create(config, dataset)
        else:
            self.model: KgeModel = model
        self.loss = KgeLoss.create(config)
        self.abort_on_nan: bool = config.get("train.abort_on_nan")
        self.batch_size: int = config.get("train.batch_size")
        self.device: str = self.config.get("job.device")
        self.train_split = config.get("train.split")

        if config.exists("train.optimizer_args.schedule"):
            config.set("train.optimizer_args.t_total",
                       math.ceil(self.dataset.split(self.train_split).size(0)
                                 / self.batch_size) * config.get("train.max_epochs"),
                       create=True, log=True)
        self.optimizer = KgeOptimizer.create(config, self.model)
        self.kge_lr_scheduler = KgeLRScheduler(config, self.optimizer)

        self.config.check("train.trace_level", ["batch", "epoch"])
        self.trace_batch: bool = self.config.get("train.trace_level") == "batch"
        self.epoch: int = 0
        self.valid_trace: List[Dict[str, Any]] = []
        valid_conf = config.clone()
        valid_conf.set("job.type", "eval")
        if self.config.get("valid.split") != "":
            valid_conf.set("eval.split", self.config.get("valid.split"))
        valid_conf.set("eval.trace_level", self.config.get("valid.trace_level"))
        self.valid_job = EvaluationJob.create(
            valid_conf, dataset, parent_job=self, model=self.model
        )
        self.is_prepared = False

        # evokg
        self.evokg_loader = None
        self.evokg_num_relations = None
        self.evokg_train_data_loader = None
        self.evokg_val_data_loader = None
        self.evokg_test_data_loader = None
        self.evokg_node_latest_event_time = None
        self.evokg_time_interval_transform = None
        self.evokg_embedding_updater = None
        self.evokg_combiner = None
        self.evokg_edge_model = None
        self.evokg_inter_event_time_model = None
        self.evokg_model = None
        self.evokg_static_entity_embeds = None
        self.evokg_init_dynamic_entity_embeds = None
        self.evokg_init_dynamic_relation_embeds = None
        self.evokg_dynamic_entity_emb_post_train = None
        self.evokg_dynamic_relation_emb_post_train = None
        self.evokg_edge_optimizer = None
        self.evokg_time_optimizer = None
        self.evokg_log_root_path = None
        self.evokg_params = None
        self.G = None

        # attributes filled in by implementing classes
        self.loader = None
        self.num_examples = None
        self.type_str: Optional[str] = None

        self.last_t_loader = None
        self.last_t_num_examples = None

        #: Hooks run after training for an epoch.
        #: Signature: job, trace_entry
        self.post_epoch_hooks: List[Callable[[Job, Dict[str, Any]], Any]] = []

        #: Hooks run before starting a batch.
        #: Signature: job
        self.pre_batch_hooks: List[Callable[[Job], Any]] = []

        #: Hooks run before outputting the trace of a batch. Can modify trace entry.
        #: Signature: job, trace_entry
        self.post_batch_trace_hooks: List[Callable[[Job, Dict[str, Any]], Any]] = []

        #: Hooks run before outputting the trace of an epoch. Can modify trace entry.
        #: Signature: job, trace_entry
        self.post_epoch_trace_hooks: List[Callable[[Job, Dict[str, Any]], Any]] = []

        #: Hooks run after a validation job.
        #: Signature: job, trace_entry
        self.post_valid_hooks: List[Callable[[Job, Dict[str, Any]], Any]] = []

        #: Hooks run after training
        #: Signature: job, trace_entry
        self.post_train_hooks: List[Callable[[Job, Dict[str, Any]], Any]] = []

        if self.__class__ == TrainingJob:
            for f in Job.job_created_hooks:
                f(self)

        self.model.train()

    @staticmethod
    def create(
            config: Config, dataset: Dataset, parent_job: Job = None, model=None
    ) -> "TrainingJob":
        """Factory method to create a training job."""
        if config.get("train.type") == "KvsAll":
            return TrainingJobKvsAll(config, dataset, parent_job, model=model)
        elif config.get("train.type") == "negative_sampling":
            return TrainingJobNegativeSampling(config, dataset, parent_job, model=model)
        elif config.get("train.type") == "1vsAll":
            return TrainingJob1vsAll(config, dataset, parent_job, model=model)
        else:
            # perhaps TODO: try class with specified name -> extensibility
            raise ValueError("train.type")

    def evokg_model_init(self):
        G = data.load_temporal_knowledge_graph(self.config.get("evokg.graph"))
        self.G = G
        self.num_relations = G.num_relations
        self.config.log("loading evokg Graph data......")
        self.config.log("\n" + "=" * 80 + "\n"
                                          f"[{self.config.get('evokg.graph')}]\n"
                                          f"# nodes={G.number_of_nodes()}\n"
                                          f"# edges={G.number_of_edges()}\n"
                                          f"# relations={G.num_relations}\n" + "=" * 80 + "\n")
        collate_fn = partial(utils.collate_fn, G=G)
        self.evokg_train_data_loader = DataLoader(G.train_times, shuffle=False, collate_fn=collate_fn)
        self.evokg_val_data_loader = DataLoader(G.val_times, shuffle=False, collate_fn=collate_fn)
        self.evokg_test_data_loader = DataLoader(G.test_times, shuffle=False, collate_fn=collate_fn)

        self.valid_job.evokg_val_data_loader = self.evokg_val_data_loader
        self.valid_job.evokg_test_data_loader = self.evokg_test_data_loader

        """Model"""
        # 形状为 (G.number_of_nodes(), G.number_of_nodes() + 1, 2)
        self.evokg_node_latest_event_time = torch.zeros(G.number_of_nodes(), G.number_of_nodes() + 1, 2,
                                                        dtype=settings.INTER_EVENT_TIME_DTYPE)
        # 对数变换通常用于将时间间隔的分布变得更均匀，尤其是在时间间隔跨度较大时。
        self.evokg_time_interval_transform = TimeIntervalTransform(
            log_transform=self.config.get("evokg.time_interval_log_transform"))

        # entity的embedding更新器
        self.evokg_embedding_updater = EmbeddingUpdater(G.number_of_nodes(),
                                                        self.config.get("evokg.static_entity_embed_dim"),
                                                        self.config.get("evokg.structural_dynamic_entity_embed_dim"),
                                                        self.config.get("evokg.temporal_dynamic_entity_embed_dim"),
                                                        self.config.get("evokg.embedding_updater_structural_gconv"),
                                                        # 没有
                                                        self.config.get("evokg.embedding_updater_temporal_gconv"),
                                                        self.evokg_node_latest_event_time,
                                                        G.num_relations,
                                                        self.config.get("evokg.rel_embed_dim"),
                                                        num_gconv_layers=self.config.get("evokg.num_gconv_layers"),
                                                        num_rnn_layers=self.config.get("evokg.num_rnn_layers"),
                                                        time_interval_transform=self.evokg_time_interval_transform,
                                                        dropout=self.config.get("evokg.dropout"),
                                                        activation=activation_string(self.config.get(
                                                            "evokg.embedding_updater_activation")),
                                                        graph_name=self.config.get("evokg.graph")).to(self.device)
        if self.config.get("evokg.static_dynamic_combine_mode") == "static_only":
            assert self.config.get("evokg.embedding_updater_structural_gconv") is None, self.config.get(
                "evokg.embedding_updater_structural_gconv")
            assert self.config.get("evokg.embedding_updater_temporal_gconv") is None, self.config.get(
                "evokg.embedding_updater_temporal_gconv")

        self.evokg_combiner = Combiner(
            self.config.get("evokg.static_entity_embed_dim"),
            self.config.get("evokg.structural_dynamic_entity_embed_dim"),
            self.config.get("evokg.static_dynamic_combine_mode"),
            # self.config.get("evokg.combiner_gconv"),
            None,
            G.num_relations,
            self.config.get("evokg.dropout"),
            self.config.get("evokg.combiner_activation"),
        ).to(self.device)

        # 边模型
        self.evokg_edge_model = EdgeModel(G.number_of_nodes(),
                                          G.num_relations,
                                          self.config.get("evokg.rel_embed_dim"),
                                          self.evokg_combiner,
                                          dropout=self.config.get("evokg.dropout")).to(self.device)

        # 时间间隔模型
        self.evokg_inter_event_time_model = InterEventTimeModel(
            dynamic_entity_embed_dim=self.config.get("evokg.temporal_dynamic_entity_embed_dim"),
            static_entity_embed_dim=self.config.get("evokg.static_entity_embed_dim"),
            num_rels=G.num_relations,
            rel_embed_dim=self.config.get("evokg.rel_embed_dim"),
            num_mix_components=self.config.get("evokg.num_mix_components"),
            time_interval_transform=self.evokg_time_interval_transform,
            inter_event_time_mode=self.config.get("evokg.inter_event_time_mode"),
            dropout=self.config.get("evokg.dropout"))

        self.evokg_model = Model(self.evokg_embedding_updater, self.evokg_combiner, self.evokg_edge_model, self.evokg_inter_event_time_model,
                                 self.evokg_node_latest_event_time).to(
            self.device)

        """Static and dynamic entity embeddings"""
        self.evokg_static_entity_embeds = MultiAspectEmbedding(
            structural=get_embedding(G.num_nodes(), self.config.get("evokg.static_entity_embed_dim"), zero_init=False),
            temporal=get_embedding(G.num_nodes(), self.config.get("evokg.static_entity_embed_dim"), zero_init=False),
        )
        # 论文中page4 equation(6)下的zero_initialized
        self.evokg_init_dynamic_entity_embeds = MultiAspectEmbedding(
            structural=get_embedding(G.num_nodes(), [self.config.get("evokg.num_rnn_layers"), self.config.get("evokg.structural_dynamic_entity_embed_dim")],
                                     zero_init=True),
            temporal=get_embedding(G.num_nodes(), [self.config.get("evokg.num_rnn_layers"), self.config.get("evokg.temporal_dynamic_entity_embed_dim"), 2],
                                   zero_init=True),
        )
        self.evokg_init_dynamic_relation_embeds = MultiAspectEmbedding(
            structural=get_embedding(G.num_relations, [self.config.get("evokg.num_rnn_layers"), self.config.get("evokg.rel_embed_dim"), 2], zero_init=True),
            temporal=get_embedding(G.num_relations, [self.config.get("evokg.num_rnn_layers"), self.config.get("evokg.rel_embed_dim"), 2], zero_init=True),
        )
        self.evokg_log_root_path = get_log_root_path(self.config.get("evokg.graph"), self.config.get("evokg.log_dir"))

        self.evokg_params = list(self.evokg_model.parameters()) + [
            self.evokg_static_entity_embeds.structural, self.evokg_static_entity_embeds.temporal,
            self.evokg_init_dynamic_entity_embeds.structural, self.evokg_init_dynamic_entity_embeds.temporal,
            self.evokg_init_dynamic_relation_embeds.structural, self.evokg_init_dynamic_relation_embeds.temporal,
        ]
        self.evokg_edge_optimizer = torch.optim.AdamW(self.evokg_params, lr=self.config.get("evokg.lr"), weight_decay=self.config.get("evokg.weight_decay"))
        self.evokg_time_optimizer = torch.optim.AdamW(self.evokg_params, lr=self.config.get("evokg.lr"), weight_decay=self.config.get("evokg.weight_decay"))


    def evokg_compute_loss(self, model, loss, batch_G, static_entity_emb, dynamic_entity_emb, dynamic_relation_emb, args=None, batch_eid=None):
        assert all([emb.device == torch.device('cpu') for emb in dynamic_entity_emb]), [emb.device for emb in
                                                                                        dynamic_entity_emb]

        if batch_eid is not None:
            assert len(batch_eid) > 0, batch_eid.shape
            sub_batch_G = dgl.edge_subgraph(batch_G, batch_eid.type(settings.DGL_GRAPH_ID_TYPE), preserve_nodes=False)
            sub_batch_G.ndata[dgl.NID] = batch_G.ndata[dgl.NID][
                sub_batch_G.ndata[dgl.NID].long()]  # map nid in sub_batch_G to nid in the full graph
            sub_batch_G = sub_batch_G.to(self.device)

            batch_eid = None  # this is needed to NOT perform further edge selection in the loss functions below
        else:
            sub_batch_G = batch_G.to(self.device)
        sub_batch_G.num_relations = batch_G.num_relations
        sub_batch_G.num_all_nodes = batch_G.num_all_nodes

        loss_dict = {}
        """Edge loss"""
        if loss in ['edge', 'both']:
            sub_batch_G_structural_static_entity_emb = static_entity_emb.structural[
                sub_batch_G.ndata[dgl.NID].long()].to(self.device)
            # [352， 200] 从dynamic_entity_emb中取出当前batch_G中节点的嵌入
            sub_batch_G_structural_dynamic_entity_emb = dynamic_entity_emb.structural[
                                                            sub_batch_G.ndata[dgl.NID].long()][:, -1, :].to(
                self.device)  # [:, -1, :] to retrieve last hidden from rnn 从rnn中检索最后一个隐藏项
            sub_batch_G_combined_emb = model.combiner(sub_batch_G_structural_static_entity_emb,
                                                      sub_batch_G_structural_dynamic_entity_emb,
                                                      sub_batch_G)
            # [240, 200, 2] 所有关系的嵌入，这里的2应该表示关系和逆关系
            structural_dynamic_relation_emb = dynamic_relation_emb.structural[:, -1, :, :].to(
                self.device)  # [:, -1, :, :] to retrieve last hidden from rnn

            # equation(15)
            edge_LL = model.edge_model(sub_batch_G, sub_batch_G_combined_emb, eid=batch_eid,
                                       static_emb=sub_batch_G_structural_static_entity_emb,
                                       dynamic_emb=sub_batch_G_structural_dynamic_entity_emb,
                                       dynamic_relation_emb=structural_dynamic_relation_emb)
            loss_dict['edge'] = -edge_LL

        """Inter-event time loss"""
        if loss in ['time', 'both']:  # 这个是做时间预测的部分，link-pred不用
            sub_batch_G_temporal_static_entity_emb = static_entity_emb.temporal[sub_batch_G.ndata[dgl.NID].long()].to(
                self.device)
            sub_batch_G_temporal_dynamic_entity_emb = dynamic_entity_emb.temporal[sub_batch_G.ndata[dgl.NID].long()][:,
                                                      -1, :, :].to(
                self.device)  # [:, -1, :, :] to retrieve last hidden from rnn
            temporal_dynamic_relation_emb = dynamic_relation_emb.temporal[:, -1, :, :].to(
                self.device)  # [:, -1, :, :] to retrieve last hidden from rnn

            # equation(16)
            inter_event_time_LL = model.inter_event_time_model.log_prob_density(
                sub_batch_G,
                sub_batch_G_temporal_dynamic_entity_emb,
                sub_batch_G_temporal_static_entity_emb,
                temporal_dynamic_relation_emb,
                model.node_latest_event_time,
                batch_eid,
                reduction='mean'
            )
            loss_dict['time'] = -inter_event_time_LL

        return loss_dict



    def run(self) -> None:
        """Start/resume the training job and run to completion."""
        self.config.log("Starting training...")
        checkpoint_every = self.config.get("train.checkpoint.every")
        checkpoint_keep = self.config.get("train.checkpoint.keep")
        metric_name = self.config.get("valid.metric")
        patience = self.config.get("valid.early_stopping.patience")
        # evokg data load
        self.evokg_model_init()
        while True:
            # checking for model improvement according to metric_name
            # and do early stopping and keep the best checkpoint
            if (
                    len(self.valid_trace) > 0
                    and self.valid_trace[-1]["epoch"] == self.epoch
            ):
                best_index = max(
                    range(len(self.valid_trace)),
                    key=lambda index: self.valid_trace[index][metric_name],
                )
                if best_index == len(self.valid_trace) - 1:
                    self.save(self.config.checkpoint_file("best"))
                if (
                        patience > 0
                        and len(self.valid_trace) > patience
                        and best_index < len(self.valid_trace) - patience
                ):
                    self.config.log(
                        "Stopping early ({} did not improve over best result ".format(
                            metric_name
                        )
                        + "in the last {} validation runs).".format(patience)
                    )
                    break
                if self.epoch > self.config.get(
                        "valid.early_stopping.min_threshold.epochs"
                ) and self.valid_trace[best_index][metric_name] < self.config.get(
                    "valid.early_stopping.min_threshold.metric_value"
                ):
                    self.config.log(
                        "Stopping early ({} did not achieve min treshold after {} epochs".format(
                            metric_name, self.epoch
                        )
                    )
                    break

            # should we stop?
            if self.epoch >= self.config.get("train.max_epochs"):
                self.config.log("Maximum number of epochs reached.")
                break

            # start a new epoch
            self.epoch += 1
            self.config.log("Starting epoch {}...".format(self.epoch))
            trace_entry = self.run_epoch()
            for f in self.post_epoch_hooks:
                f(self, trace_entry)
            self.config.log("Finished epoch {}.".format(self.epoch))

            # update model metadata
            self.model.meta["train_job_trace_entry"] = self.trace_entry
            self.model.meta["train_epoch"] = self.epoch
            self.model.meta["train_config"] = self.config
            self.model.meta["train_trace_entry"] = trace_entry

            # validate and update learning rate
            if (
                    self.config.get("valid.every") > 0
                    and self.epoch % self.config.get("valid.every") == 0
            ):
                self.valid_job.epoch = self.epoch
                self.valid_job.evokg_dynamic_entity_emb_post_train = self.evokg_dynamic_entity_emb_post_train
                self.valid_job.evokg_dynamic_relation_emb_post_train = self.evokg_dynamic_relation_emb_post_train
                self.valid_job.evokg_model = self.evokg_model
                self.valid_job.G = self.G
                self.valid_job.evokg_static_entity_embeds = self.evokg_static_entity_embeds

                trace_entry = self.valid_job.run()
                self.valid_trace.append(trace_entry)
                for f in self.post_valid_hooks:
                    f(self, trace_entry)
                self.model.meta["valid_trace_entry"] = trace_entry

                # metric-based scheduler step
                self.kge_lr_scheduler.step(trace_entry[metric_name])
            else:
                self.kge_lr_scheduler.step()

            # create checkpoint and delete old one, if necessary
            self.save(self.config.checkpoint_file(self.epoch))
            if self.epoch > 1:
                delete_checkpoint_epoch = -1
                if checkpoint_every == 0:
                    # do not keep any old checkpoints
                    delete_checkpoint_epoch = self.epoch - 1
                elif (self.epoch - 1) % checkpoint_every != 0:
                    # delete checkpoints that are not in the checkpoint.every schedule
                    delete_checkpoint_epoch = self.epoch - 1
                elif checkpoint_keep > 0:
                    # keep a maximum number of checkpoint_keep checkpoints
                    delete_checkpoint_epoch = (
                            self.epoch - 1 - checkpoint_every * checkpoint_keep
                    )
                if delete_checkpoint_epoch > 0:
                    if os.path.exists(
                            self.config.checkpoint_file(delete_checkpoint_epoch)
                    ):
                        self.config.log(
                            "Removing old checkpoint {}...".format(
                                self.config.checkpoint_file(delete_checkpoint_epoch)
                            )
                        )
                        os.remove(self.config.checkpoint_file(delete_checkpoint_epoch))
                    else:
                        self.config.log(
                            "Could not delete old checkpoint {}, does not exits.".format(
                                self.config.checkpoint_file(delete_checkpoint_epoch)
                            )
                        )

        for f in self.post_train_hooks:
            f(self, trace_entry)
        self.trace(event="train_completed")

    def save(self, filename) -> None:
        """Save current state to specified file"""
        self.config.log("Saving checkpoint to {}...".format(filename))
        checkpoint = self.save_to({})
        torch.save(
            checkpoint, filename,
        )

    def save_to(self, checkpoint: Dict) -> Dict:
        """Adds trainjob specific information to the checkpoint"""
        train_checkpoint = {
            "type": "train",
            "epoch": self.epoch,
            "valid_trace": self.valid_trace,
            "model": self.model.save(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "lr_scheduler_state_dict": self.kge_lr_scheduler.state_dict(),
            "job_id": self.job_id,
        }
        train_checkpoint = self.config.save_to(train_checkpoint)
        checkpoint.update(train_checkpoint)
        return checkpoint

    def _load(self, checkpoint: Dict) -> str:
        if checkpoint["type"] != "train":
            raise ValueError("Training can only be continued on trained checkpoints")
        self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        if "lr_scheduler_state_dict" in checkpoint:
            # new format
            self.kge_lr_scheduler.load_state_dict(checkpoint["lr_scheduler_state_dict"])
        self.epoch = checkpoint["epoch"]
        self.valid_trace = checkpoint["valid_trace"]
        self.model.train()
        self.resumed_from_job_id = checkpoint.get("job_id")
        self.trace(
            event="job_resumed", epoch=self.epoch, checkpoint_file=checkpoint["file"],
        )
        self.config.log(
            "Resuming training from {} of job {}".format(
                checkpoint["file"], self.resumed_from_job_id
            )
        )

    def run_epoch(self) -> Dict[str, Any]:
        "Runs an epoch and returns a trace entry."

        # prepare the job is not done already
        if not self.is_prepared:
            # todo: 在_prepare()中加载evokg中的loader
            self._prepare()
            self.model.prepare_job(self)  # let the model add some hooks
            self.is_prepared = True

        # variables that record various statitics
        sum_loss = 0.0
        sum_penalty = 0.0
        sum_penalties = defaultdict(lambda: 0.0)
        epoch_time = -time.time()
        prepare_time = 0.0
        forward_time = 0.0
        backward_time = 0.0
        optimizer_time = 0.0

        update_freq = self.config.get("train.update_freq")
        # process each batch
        # todo: eceformer的loader修改为只要最后一个时间戳的loader
        self.evokg_model.train()
        evokg_epoch_start_time = time.time()

        dynamic_entity_emb_post_train, dynamic_relation_emb_post_train = None, None

        self.evokg_model.node_latest_event_time.zero_()
        self.evokg_node_latest_event_time.zero_()
        dynamic_entity_emb = self.evokg_init_dynamic_entity_embeds  # 这是 t_i^{*, t-1} 上一时间的特征
        dynamic_relation_emb = self.evokg_init_dynamic_relation_embeds

        num_train_batches = len(self.evokg_train_data_loader)
        train_tqdm = tqdm(self.evokg_train_data_loader)

        epoch_train_loss_dict = defaultdict(list)
        batch_train_loss = 0
        batches_train_loss_dict = defaultdict(list)
        for batch_i, (prior_G, batch_G, cumul_G, batch_times) in enumerate(train_tqdm):
            train_tqdm.set_description(f"[Training / epoch-{self.epoch} / batch-{batch_i}]")
            last_batch = batch_i == num_train_batches - 1

            # Based on the current entity embeddings, predict edges in batch_G and compute training loss
            batch_train_loss_dict = self.evokg_compute_loss(self.evokg_model, self.config.get("evokg.optimize"), batch_G, self.evokg_static_entity_embeds,
                                                 dynamic_entity_emb, dynamic_relation_emb)
            batch_train_loss += sum(batch_train_loss_dict.values())

            for loss_term, loss_val in batch_train_loss_dict.items():
                epoch_train_loss_dict[loss_term].append(loss_val.item())
                batches_train_loss_dict[loss_term].append(loss_val.item())

            if batch_i > 0 and ((batch_i % self.config.get("evokg.rnn_truncate_every") == 0) or last_batch):
                # noinspection PyUnresolvedReferences
                batch_train_loss.backward()
                batch_train_loss = 0

                if self.config.get("evokg.optimize") in ['edge', 'both']:
                    self.evokg_edge_optimizer.step()
                    self.evokg_edge_optimizer.zero_grad()
                if self.config.get("evokg.optimize") in ['time', 'both']:
                    self.evokg_time_optimizer.step()
                    self.evokg_time_optimizer.zero_grad()
                torch.cuda.empty_cache()

                if self.config.get("evokg.embedding_updater_structural_gconv") or self.config.get("evokg.embedding_updater_temporal_gconv"):
                    for emb in dynamic_entity_emb + dynamic_relation_emb:
                        emb.detach_()

                tqdm.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')} [Epoch {self.epoch:03d}-Batch {batch_i:03d}] "
                           f"batch train loss total={sum([sum(l) for l in batches_train_loss_dict.values()]):.4f} | "
                           f"{', '.join([f'{loss_term}={sum(loss_cumul):.4f}' for loss_term, loss_cumul in batches_train_loss_dict.items()])}")
                batches_train_loss_dict = defaultdict(list)

            dynamic_entity_emb, dynamic_relation_emb = \
                self.evokg_model.embedding_updater.forward(prior_G, batch_G, cumul_G, self.evokg_static_entity_embeds,
                                                dynamic_entity_emb, dynamic_relation_emb, self.device)

            # eval debug
            # self.evokg_dynamic_entity_emb_post_train = dynamic_entity_emb  # 经过最后一个时间戳后的所有实体的嵌入特征
            # self.evokg_dynamic_relation_emb_post_train = dynamic_relation_emb  # 经过最后一个时间戳后的所有关系的嵌入特征
            # break

            if last_batch:
                # 训练完了一个graph中的所有时刻的图
                self.evokg_dynamic_entity_emb_post_train = dynamic_entity_emb  # 经过最后一个时间戳后的所有实体的嵌入特征
                self.evokg_dynamic_relation_emb_post_train = dynamic_relation_emb  # 经过最后一个时间戳后的所有关系的嵌入特征

        # for batch_index, batch in enumerate(self.loader):
        for batch_index, batch in enumerate(self.last_t_loader):  # batch: batch["triples"] = [512, 4]
            for f in self.pre_batch_hooks:
                f(self)

            batch["evokg_embs"] = {
                "entity": self.evokg_dynamic_entity_emb_post_train.structural[:, -1, :].to(self.device),
                "rel": self.evokg_dynamic_relation_emb_post_train.structural[:, -1, :].to(self.device)
            }

            # process batch (preprocessing + forward pass + backward pass on loss)
            if batch_index % update_freq == 0:
                self.optimizer.zero_grad()
            batch_result: TrainingJob._ProcessBatchResult = self._process_batch(
                batch_index, batch
            )
            sum_loss += batch_result.avg_loss * batch_result.size

            # determine penalty terms (forward pass)
            batch_forward_time = batch_result.forward_time - time.time()
            penalties_torch = self.model.penalty(
                epoch=self.epoch,
                batch_index=batch_index,
                num_batches=len(self.loader),
                batch=batch,
            )
            batch_forward_time += time.time()

            # backward pass on penalties
            batch_backward_time = batch_result.backward_time - time.time()
            penalty = 0.0
            for index, (penalty_key, penalty_value_torch) in enumerate(penalties_torch):
                penalty_value_torch.backward()
                penalty += penalty_value_torch.item()
                sum_penalties[penalty_key] += penalty_value_torch.item()
            sum_penalty += penalty
            batch_backward_time += time.time()

            # determine full cost
            cost_value = batch_result.avg_loss + penalty

            # abort on nan
            if self.abort_on_nan and math.isnan(cost_value):
                raise FloatingPointError("Cost became nan, aborting training job")

            # TODO # visualize graph
            # if (
            #     self.epoch == 1
            #     and batch_index == 0
            #     and self.config.get("train.visualize_graph")
            # ):
            #     from torchviz import make_dot

            #     f = os.path.join(self.config.folder, "cost_value")
            #     graph = make_dot(cost_value, params=dict(self.model.named_parameters()))
            #     graph.save(f"{f}.gv")
            #     graph.render(f)  # needs graphviz installed
            #     self.config.log("Exported compute graph to " + f + ".{gv,pdf}")

            # print memory stats
            if self.epoch == 1 and batch_index == 0:
                if self.device.startswith("cuda"):
                    self.config.log(
                        "CUDA memory after first batch: allocated={:14,} "
                        "cached={:14,} max_allocated={:14,}".format(
                            torch.cuda.memory_allocated(self.device),
                            torch.cuda.memory_cached(self.device),
                            torch.cuda.max_memory_allocated(self.device),
                        )
                    )

            # update parameters
            batch_optimizer_time = -time.time()
            if batch_index % update_freq == update_freq - 1:
                self.optimizer.step()
            batch_optimizer_time += time.time()

            # tracing/logging
            if self.trace_batch:
                batch_trace = {
                    "type": self.type_str,
                    "scope": "batch",
                    "epoch": self.epoch,
                    "split": self.train_split,
                    "batch": batch_index,
                    "size": batch_result.size,
                    "batches": len(self.loader),
                    "lr": self.optimizer.get_lr() if hasattr(self.optimizer, 'get_lr') else [group["lr"] for group in
                                                                                             self.optimizer.param_groups],
                    "avg_loss": batch_result.avg_loss,
                    "penalties": [p.item() for k, p in penalties_torch],
                    "penalty": penalty,
                    "cost": cost_value,
                    "prepare_time": batch_result.prepare_time,
                    "forward_time": batch_forward_time,
                    "backward_time": batch_backward_time,
                    "optimizer_time": batch_optimizer_time,
                }
                for f in self.post_batch_trace_hooks:
                    f(self, batch_trace)
                self.trace(**batch_trace, event="batch_completed")
            self.config.print(
                (
                        "\r"  # go back
                        + "{}  batch{: "
                        + str(1 + int(math.ceil(math.log10(len(self.loader)))))
                        + "d}/{}"
                        + ", avg_loss {:.4E}, penalty {:.4E}, cost {:.4E}, time {:6.2f}s"
                        + "\033[K"  # clear to right
                ).format(
                    self.config.log_prefix,
                    batch_index,
                    len(self.loader) - 1,
                    batch_result.avg_loss,
                    penalty,
                    cost_value,
                    batch_result.prepare_time
                    + batch_forward_time
                    + batch_backward_time
                    + batch_optimizer_time,
                ),
                end="",
                flush=True,
            )

            # update times
            prepare_time += batch_result.prepare_time
            forward_time += batch_forward_time
            backward_time += batch_backward_time
            optimizer_time += batch_optimizer_time

        # all done; now trace and log
        epoch_time += time.time()
        self.config.print("\033[2K\r", end="", flush=True)  # clear line and go back

        other_time = (
                epoch_time - prepare_time - forward_time - backward_time - optimizer_time
        )
        trace_entry = dict(
            type=self.type_str,
            scope="epoch",
            epoch=self.epoch,
            split=self.train_split,
            batches=len(self.loader),
            size=self.num_examples,
            lr=self.optimizer.get_lr() if hasattr(self.optimizer, 'get_lr') else [
                group["lr"] for group in self.optimizer.param_groups],
            avg_loss=sum_loss / self.num_examples,
            avg_penalty=sum_penalty / len(self.loader),
            avg_penalties={k: p / len(self.loader) for k, p in sum_penalties.items()},
            avg_cost=sum_loss / self.num_examples + sum_penalty / len(self.loader),
            epoch_time=epoch_time,
            prepare_time=prepare_time,
            forward_time=forward_time,
            backward_time=backward_time,
            optimizer_time=optimizer_time,
            other_time=other_time,
            event="epoch_completed",
        )
        for f in self.post_epoch_trace_hooks:
            f(self, trace_entry)
        trace_entry = self.trace(**trace_entry, echo=True, echo_prefix="  ", log=True)
        return trace_entry

    def _prepare(self):
        """Prepare this job for running.

        Sets (at least) the `loader`, `num_examples`, and `type_str` attributes of this
        job to a data loader, number of examples per epoch, and a name for the trainer,
        repectively.

        Guaranteed to be called exactly once before running the first epoch.

        """
        raise NotImplementedError

    @dataclass
    class _ProcessBatchResult:
        """Result of running forward+backward pass on a batch."""

        avg_loss: float
        size: int
        prepare_time: float
        forward_time: float
        backward_time: float

    def _process_batch(
            self, batch_index: int, batch
    ) -> "TrainingJob._ProcessBatchResult":
        "Run forward and backward pass on batch and return results."
        raise NotImplementedError


class TrainingJobKvsAll(TrainingJob):
    """Train with examples consisting of a query and its answers.

    Terminology:
    - Query type: which queries to ask (sp_, s_o, and/or _po), can be configured via
      configuration key `KvsAll.query_type` (which see)
    - Query: a particular query, e.g., (John,marriedTo) of type sp_
    - Labels: list of true answers of a query (e.g., [Jane])
    - Example: a query + its labels, e.g., (John,marriedTo), [Jane]
    """

    def __init__(self, config, dataset, parent_job=None, model=None):
        super().__init__(config, dataset, parent_job, model=model)
        self.label_smoothing = config.check_range(
            "KvsAll.label_smoothing", float("-inf"), 1.0, max_inclusive=False
        )
        if self.label_smoothing < 0:
            if config.get("train.auto_correct"):
                config.log(
                    "Setting label_smoothing to 0, "
                    "was set to {}.".format(self.label_smoothing)
                )
                self.label_smoothing = 0
            else:
                raise Exception(
                    "Label_smoothing was set to {}, "
                    "should be at least 0.".format(self.label_smoothing)
                )
        elif self.label_smoothing > 0 and self.label_smoothing <= (
                1.0 / dataset.num_entities()
        ):
            if config.get("train.auto_correct"):
                # just to be sure it's used correctly
                config.log(
                    "Setting label_smoothing to 1/num_entities = {}, "
                    "was set to {}.".format(
                        1.0 / dataset.num_entities(), self.label_smoothing
                    )
                )
                self.label_smoothing = 1.0 / dataset.num_entities()
            else:
                raise Exception(
                    "Label_smoothing was set to {}, "
                    "should be at least {}.".format(
                        self.label_smoothing, 1.0 / dataset.num_entities()
                    )
                )

        config.log("Initializing 1-to-N training job...")
        self.type_str = "KvsAll"

        if self.__class__ == TrainingJobKvsAll:
            for f in Job.job_created_hooks:
                f(self)

    def _prepare(self):
        from kge.indexing import index_KvsAll_to_torch

        # determine enabled query types
        self.query_types = [
            key
            for key, enabled in self.config.get("KvsAll.query_types").items()
            if enabled
        ]

        # for each query type: list of queries
        self.queries = {}

        # for each query type: list of all labels (concatenated across queries)
        self.labels = {}

        # for each query type: list of starting offset of labels in self.labels. The
        # labels for the i-th query of query_type are in labels[query_type] in range
        # label_offsets[query_type][i]:label_offsets[query_type][i+1]
        self.label_offsets = {}

        # for each query type (ordered as in self.query_types), index right after last
        # example of that type in the list of all examples
        self.query_end_index = []

        # construct relevant data structures
        self.num_examples = 0
        for query_type in self.query_types:
            index_type = (
                "sp_to_o"
                if query_type == "sp_"
                else ("so_to_p" if query_type == "s_o" else "po_to_s")
            )
            index = self.dataset.index(f"{self.train_split}_{index_type}")
            self.num_examples += len(index)
            self.query_end_index.append(self.num_examples)

            # Convert indexes to pytorch tensors (as described above).
            (
                self.queries[query_type],
                self.labels[query_type],
                self.label_offsets[query_type],
            ) = index_KvsAll_to_torch(index)

        # create dataloader
        self.loader = torch.utils.data.DataLoader(
            range(self.num_examples),
            collate_fn=self._get_collate_fun(),
            shuffle=True,
            batch_size=self.batch_size,
            num_workers=self.config.get("train.num_workers"),
            worker_init_fn=_generate_worker_init_fn(self.config),
            pin_memory=self.config.get("train.pin_memory"),
        )

    def _get_collate_fun(self):
        # create the collate function
        def collate(batch):
            """For a batch of size n, returns a dictionary of:

            - queries: nx2 tensor, row = query (sp, po, or so indexes)
            - label_coords: for each query, position of true answers (an Nx2 tensor,
              first columns holds query index, second colum holds index of label)
            - query_type_indexes (vector of size n holding the query type of each query)
            - triples (all true triples in the batch; e.g., needed for weighted
              penalties)

            """

            # count how many labels we have across the entire batch
            num_ones = 0
            for example_index in batch:
                start = 0
                for query_type_index, query_type in enumerate(self.query_types):
                    end = self.query_end_index[query_type_index]
                    if example_index < end:
                        example_index -= start
                        num_ones += self.label_offsets[query_type][example_index + 1]
                        num_ones -= self.label_offsets[query_type][example_index]
                        break
                    start = end

            # now create the batch elements
            queries_batch = torch.zeros([len(batch), 2], dtype=torch.long)
            query_type_indexes_batch = torch.zeros([len(batch)], dtype=torch.long)
            label_coords_batch = torch.zeros([num_ones, 2], dtype=torch.int)
            triples_batch = torch.zeros([num_ones, 3], dtype=torch.long)
            current_index = 0
            for batch_index, example_index in enumerate(batch):
                start = 0
                for query_type_index, query_type in enumerate(self.query_types):
                    end = self.query_end_index[query_type_index]
                    if example_index < end:
                        example_index -= start
                        query_type_indexes_batch[batch_index] = query_type_index
                        queries = self.queries[query_type]
                        label_offsets = self.label_offsets[query_type]
                        labels = self.labels[query_type]
                        if query_type == "sp_":
                            query_col_1, query_col_2, target_col = S, P, O
                        elif query_type == "s_o":
                            query_col_1, target_col, query_col_2 = S, P, O
                        else:
                            target_col, query_col_1, query_col_2 = S, P, O
                        break
                    start = end

                queries_batch[batch_index,] = queries[example_index]
                start = label_offsets[example_index]
                end = label_offsets[example_index + 1]
                size = end - start
                label_coords_batch[
                current_index: (current_index + size), 0
                ] = batch_index
                label_coords_batch[current_index: (current_index + size), 1] = labels[
                                                                               start:end
                                                                               ]
                triples_batch[
                current_index: (current_index + size), query_col_1
                ] = queries[example_index][0]
                triples_batch[
                current_index: (current_index + size), query_col_2
                ] = queries[example_index][1]
                triples_batch[
                current_index: (current_index + size), target_col
                ] = labels[start:end]
                current_index += size

            # all done
            return {
                "queries": queries_batch,
                "label_coords": label_coords_batch,
                "query_type_indexes": query_type_indexes_batch,
                "triples": triples_batch,
            }

        return collate

    def _process_batch(self, batch_index, batch) -> TrainingJob._ProcessBatchResult:
        # prepare
        prepare_time = -time.time()
        queries_batch = batch["queries"].to(self.device)
        batch_size = len(queries_batch)
        label_coords_batch = batch["label_coords"].to(self.device)
        query_type_indexes_batch = batch["query_type_indexes"]

        # in this method, example refers to the index of an example in the batch, i.e.,
        # it takes values in 0,1,...,batch_size-1
        examples_for_query_type = {}
        for query_type_index, query_type in enumerate(self.query_types):
            examples_for_query_type[query_type] = (
                (query_type_indexes_batch == query_type_index)
                .nonzero()
                .to(self.device)
                .view(-1)
            )

        labels_batch = kge.job.util.coord_to_sparse_tensor(
            batch_size,
            max(self.dataset.num_entities(), self.dataset.num_relations()),
            label_coords_batch,
            self.device,
        ).to_dense()
        labels_for_query_type = {}
        for query_type, examples in examples_for_query_type.items():
            if query_type == "s_o":
                labels_for_query_type[query_type] = labels_batch[
                                                    examples, : self.dataset.num_relations()
                                                    ]
            else:
                labels_for_query_type[query_type] = labels_batch[
                                                    examples, : self.dataset.num_entities()
                                                    ]

        if self.label_smoothing > 0.0:
            # as in ConvE: https://github.com/TimDettmers/ConvE
            for query_type, labels in labels_for_query_type.items():
                if query_type != "s_o":  # entity targets only for now
                    labels_for_query_type[query_type] = (
                                                                1.0 - self.label_smoothing
                                                        ) * labels + 1.0 / labels.size(1)

        prepare_time += time.time()

        # forward/backward pass (sp)
        loss_value_total = 0.0
        backward_time = 0
        forward_time = 0
        for query_type, examples in examples_for_query_type.items():
            if len(examples) > 0:
                forward_time -= time.time()
                if query_type == "sp_":
                    scores = self.model.score_sp(
                        queries_batch[examples, 0], queries_batch[examples, 1]
                    )
                elif query_type == "s_o":
                    scores = self.model.score_so(
                        queries_batch[examples, 0], queries_batch[examples, 1]
                    )
                else:
                    scores = self.model.score_po(
                        queries_batch[examples, 0], queries_batch[examples, 1]
                    )
                loss_value = (
                        self.loss(scores, labels_for_query_type[query_type]) / batch_size
                )
                loss_value_total = loss_value.item()
                forward_time += time.time()
                backward_time -= time.time()
                loss_value.backward()
                backward_time += time.time()

        # all done
        return TrainingJob._ProcessBatchResult(
            loss_value_total, batch_size, prepare_time, forward_time, backward_time
        )


class TrainingJobNegativeSampling(TrainingJob):
    def __init__(self, config, dataset, parent_job=None, model=None):
        super().__init__(config, dataset, parent_job, model=model)
        self._sampler = KgeSampler.create(config, "negative_sampling", dataset)
        self.is_prepared = False
        self._implementation = self.config.check(
            "negative_sampling.implementation", ["triple", "all", "batch", "auto"],
        )
        if self._implementation == "auto":
            max_nr_of_negs = max(self._sampler.num_samples)
            if self._sampler.shared:
                self._implementation = "batch"
            elif max_nr_of_negs <= 30:
                self._implementation = "triple"
            elif max_nr_of_negs > 30:
                self._implementation = "batch"
        self._max_chunk_size = self.config.get("negative_sampling.chunk_size")

        config.log(
            "Initializing negative sampling training job with "
            "'{}' scoring function ...".format(self._implementation)
        )
        self.type_str = "negative_sampling"

        if self.__class__ == TrainingJobNegativeSampling:
            for f in Job.job_created_hooks:
                f(self)

    def _prepare(self):
        """Construct dataloader"""

        if self.is_prepared:
            return

        self.num_examples = self.dataset.split(self.train_split).size(0)
        self.loader = torch.utils.data.DataLoader(
            range(self.num_examples),
            collate_fn=self._get_collate_fun(),
            shuffle=True,
            batch_size=self.batch_size,
            num_workers=self.config.get("train.num_workers"),
            worker_init_fn=_generate_worker_init_fn(self.config),
            pin_memory=self.config.get("train.pin_memory"),
        )

        self.is_prepared = True

    def _get_collate_fun(self):
        # create the collate function
        def collate(batch):
            """For a batch of size n, returns a tuple of:

            - triples (tensor of shape [n,3], ),
            - negative_samples (list of tensors of shape [n,num_samples]; 3 elements
              in order S,P,O)
            """

            triples = self.dataset.split(self.train_split)[batch, :].long()
            # labels = torch.zeros((len(batch), self._sampler.num_negatives_total + 1))
            # labels[:, 0] = 1
            # labels = labels.view(-1)

            negative_samples = list()
            for slot in [S, P, O]:
                negative_samples.append(self._sampler.sample(triples, slot))
            return {"triples": triples, "negative_samples": negative_samples}

        return collate

    def _process_batch(self, batch_index, batch) -> TrainingJob._ProcessBatchResult:
        # prepare
        prepare_time = -time.time()
        batch_triples = batch["triples"].to(self.device)
        batch_negative_samples = [
            ns.to(self.device) for ns in batch["negative_samples"]
        ]
        batch_size = len(batch_triples)
        prepare_time += time.time()

        loss_value = 0.0
        forward_time = 0.0
        backward_time = 0.0
        labels = None

        # perform processing of batch in smaller chunks to save memory
        max_chunk_size = (
            self._max_chunk_size if self._max_chunk_size > 0 else batch_size
        )
        for chunk_number in range(math.ceil(batch_size / max_chunk_size)):
            # determine data used for this chunk
            chunk_start = max_chunk_size * chunk_number
            chunk_end = min(max_chunk_size * (chunk_number + 1), batch_size)
            negative_samples = [
                ns[chunk_start:chunk_end, :] for ns in batch_negative_samples
            ]
            triples = batch_triples[chunk_start:chunk_end, :]
            chunk_size = chunk_end - chunk_start

            # process the chunk
            for slot in [S, P, O]:
                num_samples = self._sampler.num_samples[slot]
                if num_samples <= 0:
                    continue

                # construct gold labels: first column corresponds to positives,
                # remaining columns to negatives
                if labels is None or labels.shape != torch.Size(
                        [chunk_size, 1 + num_samples]
                ):
                    prepare_time -= time.time()
                    labels = torch.zeros(
                        (chunk_size, 1 + num_samples), device=self.device
                    )
                    labels[:, 0] = 1
                    prepare_time += time.time()

                # compute corresponding scores
                scores = None
                if self._implementation == "triple":
                    # construct triples
                    prepare_time -= time.time()
                    triples_to_score = triples.repeat(1, 1 + num_samples).view(-1, 3)
                    triples_to_score[:, slot] = torch.cat(
                        (
                            triples[:, [slot]],  # positives
                            negative_samples[slot],  # negatives
                        ),
                        1,
                    ).view(-1)
                    prepare_time += time.time()

                    # and score them
                    forward_time -= time.time()
                    scores = self.model.score_spo(
                        triples_to_score[:, 0],
                        triples_to_score[:, 1],
                        triples_to_score[:, 2],
                        direction="s" if slot == S else ("o" if slot == O else "p"),
                    ).view(chunk_size, -1)
                    forward_time += time.time()
                elif self._implementation == "all":
                    # Score against all possible targets. Creates a score matrix of size
                    # [chunk_size, num_entities] or [chunk_size, num_relations]. All
                    # scores relevant for positive and negative triples are contained in
                    # this score matrix.

                    # compute all scores for slot
                    forward_time -= time.time()
                    if slot == S:
                        all_scores = self.model.score_po(triples[:, P], triples[:, O])
                    elif slot == P:
                        all_scores = self.model.score_so(triples[:, S], triples[:, O])
                    elif slot == O:
                        all_scores = self.model.score_sp(triples[:, S], triples[:, P])
                    else:
                        raise NotImplementedError
                    forward_time += time.time()

                    # determine indexes of relevant scores in scoring matrix
                    prepare_time -= time.time()
                    row_indexes = (
                        torch.arange(chunk_size, device=self.device)
                        .unsqueeze(1)
                        .repeat(1, 1 + num_samples)
                        .view(-1)
                    )  # 000 111 222; each 1+num_negative times (here: 3)
                    column_indexes = torch.cat(
                        (
                            triples[:, [slot]],  # positives
                            negative_samples[slot],  # negatives
                        ),
                        1,
                    ).view(-1)
                    prepare_time += time.time()

                    # now pick the scores we need
                    forward_time -= time.time()
                    scores = all_scores[row_indexes, column_indexes].view(
                        chunk_size, -1
                    )
                    forward_time += time.time()
                elif self._implementation == "batch":
                    # Score against all targets contained in the chunk. Creates a score
                    # matrix of size [chunk_size, unique_entities_in_slot] or
                    # [chunk_size, unique_relations_in_slot]. All scores
                    # relevant for positive and negative triples are contained in this
                    # score matrix.
                    forward_time -= time.time()
                    unique_targets, column_indexes = torch.unique(
                        torch.cat((triples[:, [slot]], negative_samples[slot]), 1).view(
                            -1
                        ),
                        return_inverse=True,
                    )

                    # compute scores for all unique targets for slot
                    if slot == S:
                        all_scores = self.model.score_po(
                            triples[:, P], triples[:, O], unique_targets
                        )
                    elif slot == P:
                        all_scores = self.model.score_so(
                            triples[:, S], triples[:, O], unique_targets
                        )
                    elif slot == O:
                        all_scores = self.model.score_sp(
                            triples[:, S], triples[:, P], unique_targets
                        )
                    else:
                        raise NotImplementedError
                    forward_time += time.time()

                    # determine indexes of relevant scores in scoring matrix
                    prepare_time -= time.time()
                    row_indexes = (
                        torch.arange(chunk_size, device=self.device)
                        .unsqueeze(1)
                        .repeat(1, 1 + num_samples)
                        .view(-1)
                    )  # 000 111 222; each 1+num_negative times (here: 3)
                    prepare_time += time.time()

                    # now pick the scores we need
                    forward_time -= time.time()
                    scores = all_scores[row_indexes, column_indexes].view(
                        chunk_size, -1
                    )
                    forward_time += time.time()

                # compute chunk loss (concluding the forward pass of the chunk)
                forward_time -= time.time()
                loss_value_torch = (
                        self.loss(scores, labels, num_negatives=num_samples) / batch_size
                )
                loss_value += loss_value_torch.item()
                forward_time += time.time()

                # backward pass for this chunk
                backward_time -= time.time()
                loss_value_torch.backward()
                backward_time += time.time()

        # all done
        return TrainingJob._ProcessBatchResult(
            loss_value, batch_size, prepare_time, forward_time, backward_time
        )


def tiny_value_of_dtype(dtype: torch.dtype):
    """
    Returns a moderately tiny value for a given PyTorch data type that is used to avoid numerical
    issues such as division by zero.
    This is different from `info_value_of_dtype(dtype).tiny` because it causes some NaN bugs.
    Only supports floating point dtypes.
    """
    if not dtype.is_floating_point:
        raise TypeError("Only supports floating point dtypes.")
    if dtype == torch.float or dtype == torch.double:
        return 1e-13
    elif dtype == torch.half:
        return 1e-4
    else:
        raise TypeError("Does not support dtype " + str(dtype))


def mask_score(vector, mask, demask):
    mask[range(len(demask)), demask] = 0
    return vector + (~mask + tiny_value_of_dtype(vector.dtype)).log()


class TrainingJob1vsAll(TrainingJob):
    """Samples SPO pairs and queries sp_ and _po, treating all other entities as negative."""

    def __init__(self, config, dataset, parent_job=None, model=None):
        super().__init__(config, dataset, parent_job, model=model)
        self.is_prepared = False
        config.log("Initializing spo training job...")
        self.type_str = "1vsAll"

        if self.__class__ == TrainingJob1vsAll:
            for f in Job.job_created_hooks:
                f(self)

    def _prepare(self):
        """Construct dataloader"""

        if self.is_prepared:
            return

        self.num_examples = self.dataset.split(self.train_split).size(0)
        self.loader = torch.utils.data.DataLoader(
            range(self.num_examples),
            collate_fn=lambda batch: {
                "triples": self.dataset.split(self.train_split)[batch, :].long()
            },
            shuffle=True,
            batch_size=self.batch_size,
            num_workers=self.config.get("train.num_workers"),
            worker_init_fn=_generate_worker_init_fn(self.config),
            pin_memory=self.config.get("train.pin_memory"),
        )

        # eceformer的dataloader只有最后一个t的图
        train_quadruple = self.dataset.split(self.train_split)
        last_t = max(train_quadruple[:, -1])
        # 取出train_quadruple中，第4列元素等于last_t的triple
        last_t_graph = train_quadruple[train_quadruple[:, -1] == last_t]
        self.last_t_num_examples = last_t_graph.size(0)
        self.last_t_loader = torch.utils.data.DataLoader(
            range(self.last_t_num_examples),
            collate_fn=lambda batch: {
                "triples": last_t_graph[batch, :].long()
            },
            shuffle=True,
            batch_size=self.batch_size,
            num_workers=self.config.get("train.num_workers"),
            worker_init_fn=_generate_worker_init_fn(self.config),
            pin_memory=self.config.get("train.pin_memory"),
        )

        self.is_prepared = True

    def _process_batch(self, batch_index, batch) -> TrainingJob._ProcessBatchResult:
        # prepare
        prepare_time = -time.time()
        triples = batch["triples"].to(self.device)
        batch_size = len(triples)
        # todo: s=triples[:, 0]/o=triples[:, 2]直接换成dynamic_entity_emb_post_train中对应的特征
        # todo: r=triples[:, 1]直接换成dynamic_relation_emb_post_train中对应的特征

        prepare_time += time.time()

        # combine two forward/backward pass to speed up
        # forward/backward pass (sp)
        forward_time = -time.time()
        # todo: 把score_sp修改为evokg_score_sp，直接传入上述得到的特征
        # loss_value_sp = self.model("score_sp", triples[:, 0], triples[:, 1], triples[:, 3],
        #                            gt_ent=triples[:, 2], gt_rel=triples[:, 1] + self.dataset.num_relations(),
        #                            gt_tim=triples[:, 3]).sum() / batch_size
        loss_value_sp = self.model("evokg_score_sp", batch["evokg_embs"], triples[:, 0], triples[:, 1], triples[:, 3],
                                   gt_ent=triples[:, 2], gt_rel=triples[:, 1] + self.dataset.num_relations(),
                                   gt_tim=triples[:, 3]).sum() / batch_size
        loss_value = loss_value_sp.item()
        forward_time += time.time()
        backward_time = -time.time()
        # loss_value_sp.backward()
        backward_time += time.time()

        # forward/backward pass (po)
        forward_time -= time.time()
        # loss_value_po = self.model("score_po", triples[:, 1], triples[:, 2], triples[:, 3],
        #                            gt_ent=triples[:, 0], gt_rel=triples[:, 1], gt_tim=triples[:, 3]).sum() / batch_size
        loss_value_po = self.model("evokg_score_po", batch["evokg_embs"], triples[:, 1], triples[:, 2], triples[:, 3],
                                   gt_ent=triples[:, 0], gt_rel=triples[:, 1], gt_tim=triples[:, 3]).sum() / batch_size
        loss_value += loss_value_po.item()
        forward_time += time.time()
        backward_time -= time.time()
        (loss_value_po + loss_value_sp).backward(retain_graph=True)
        backward_time += time.time()

        # all done
        return TrainingJob._ProcessBatchResult(
            loss_value, batch_size, prepare_time, forward_time, backward_time
        )

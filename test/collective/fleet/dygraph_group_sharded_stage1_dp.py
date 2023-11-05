# -*- coding: UTF-8 -*-

# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np

import paddle
from paddle.distributed import fleet
from paddle.distributed.fleet.utils import mix_precision_utils
from paddle.nn import Linear, ReLU

seed = 2022
epoch = 2
linear_size = 1000

np.random.seed(seed)
paddle.seed(seed)


class MLP(paddle.nn.Layer):
    def __init__(self, linear_size=1000):
        super().__init__()

        self._linear1 = Linear(linear_size, linear_size)
        self._linear2 = Linear(linear_size, linear_size)
        self._linear3 = Linear(linear_size, 10)
        self._relu = ReLU()

    def forward(self, inputs):
        y = self._linear1(inputs)
        y = self._linear2(y)
        y = self._linear3(y)
        y = self._relu(y)
        return y


class RandomDataset(paddle.io.Dataset):
    def __init__(self, num_samples=200, linear_size=1000):
        self.num_samples = num_samples
        self.linear_size = linear_size

    def __getitem__(self, idx):
        img = np.random.rand(self.linear_size).astype('float32')
        return img

    def __len__(self):
        return self.num_samples


def optimizer_setting(model, use_pure_bf16, use_main_grad):
    if use_main_grad:
        assert use_pure_bf16
        model = mix_precision_utils.MixPrecisionLayer(model, dtype="bfloat16")
    optimizer = paddle.optimizer.AdamW(
        parameters=model.parameters(),
        learning_rate=0.00001,
        weight_decay=0.00001,
        grad_clip=paddle.nn.ClipGradByGlobalNorm(clip_norm=1.0),
        multi_precision=use_pure_bf16,
    )
    if use_main_grad:
        optimizer = mix_precision_utils.MixPrecisionOptimizer(optimizer)

    return optimizer


def train_mlp(
    model,
    use_sharding_stage1=False,
    use_pure_bf16=False,
    accumulate_grad=False,
    use_main_grad=False,
    test_scaler=False,
    test_dp=False,
):
    # bf16 not support dynamic loss scaling
    # disable dynamic_loss_scaling to coverage distributed_scaler
    dynamic_loss_scaling = False
    scaler = None
    scale_loss = 1024
    if test_scaler:
        assert use_sharding_stage1
        assert not accumulate_grad
        scaler = paddle.amp.GradScaler(
            init_loss_scaling=scale_loss,
            use_dynamic_loss_scaling=dynamic_loss_scaling,
        )
        scaler = fleet.distributed_scaler(scaler)
    optimizer = optimizer_setting(
        model=model, use_pure_bf16=use_pure_bf16, use_main_grad=use_main_grad
    )

    strategy = fleet.DistributedStrategy()
    if use_pure_bf16:
        level = 'O2'
        custom_white_list = None

        amp_configs = {
            "init_loss_scaling": scale_loss,
            "use_pure_bf16": True,
            "use_dynamic_loss_scaling": dynamic_loss_scaling,
        }
        strategy.amp = True
        strategy.amp_configs = amp_configs
    else:
        level = 'O1'
        custom_white_list = [
            "matmul_v2",
            "elementwise_add",
            "relu",
            "reduce_mean",
        ]
        strategy.amp = True

    if use_sharding_stage1:
        if test_dp:
            dp_degree = 2
            sharding_degree = 2
        else:
            dp_degree = 1
            sharding_degree = 4
    else:
        dp_degree = -1
        sharding_degree = 1
        strategy.heter_ccl_mode = True

    hybrid_configs = {
        "dp_degree": dp_degree,
        "mp_degree": 1,
        "pp_degree": 1,
        "sharding_degree": sharding_degree,
    }
    strategy.hybrid_configs = hybrid_configs

    fleet.init(is_collective=True, strategy=strategy)

    model = fleet.distributed_model(model)
    if use_sharding_stage1 == 1:
        optimizer = fleet.distributed_optimizer(optimizer)

    paddle.seed(2023)
    np.random.seed(2023)
    train_loader = paddle.io.DataLoader(
        RandomDataset(),
        batch_size=100,
        shuffle=False,
        drop_last=True,
        num_workers=0,
    )

    if use_sharding_stage1 == 1:
        model.to(device="gpu")

    if not use_pure_bf16:
        for param in model.parameters():
            t = paddle.cast(
                paddle.cast(param, dtype='bfloat16'), dtype='float32'
            )
            param.set_value(t)

    losses = []
    for eop in range(epoch):
        model.train()

        for batch_id, data in enumerate(train_loader()):
            data.stop_gradient = True

            with paddle.amp.auto_cast(
                True,
                level=level,
                dtype="bfloat16",
                custom_white_list=custom_white_list,
            ):
                out = model(data)
                loss = paddle.mean(out)

            losses.append(loss)

            loss.backward()
            if not accumulate_grad:
                optimizer.step()
                optimizer.clear_grad()

        if accumulate_grad:
            optimizer.step()
            optimizer.clear_grad()

    return losses


def test_stage1_bf16_dp():
    if not paddle.amp.is_bfloat16_supported():
        return
    paddle.distributed.init_parallel_env()

    mlp = MLP()
    state_dict = mlp.state_dict()

    # stage1 + dp vs pure stage1
    mlp1 = MLP()
    mlp2 = MLP()
    mlp1.set_state_dict(state_dict)
    mlp2.set_state_dict(state_dict)
    o1_losses = train_mlp(mlp1, use_sharding_stage1=True, test_dp=True)
    o2_losses = train_mlp(
        mlp2,
        use_sharding_stage1=True,
    )
    for i in range(len(o1_losses)):
        o1_loss = o1_losses[i].detach()
        o2_loss = o2_losses[i].detach()
        np.testing.assert_array_equal(o1_loss, o2_loss)

    # stage1 + dp vs pure dp
    mlp3 = MLP()
    mlp3.set_state_dict(state_dict)
    o2_losses = train_mlp(
        mlp3,
        use_sharding_stage1=False,
    )
    for i in range(len(o1_losses)):
        o1_loss = o1_losses[i].detach()
        o2_loss = o2_losses[i].detach()
        np.testing.assert_array_equal(o1_loss, o2_loss)

    # test bf16
    mlp4 = MLP()
    mlp5 = MLP()
    mlp4.set_state_dict(state_dict)
    mlp5.set_state_dict(state_dict)
    o1_losses = train_mlp(
        mlp4,
        use_sharding_stage1=True,
        use_pure_bf16=True,
        test_dp=True,
    )
    o2_losses = train_mlp(
        mlp5,
        use_sharding_stage1=True,
        use_pure_bf16=True,
    )
    for i in range(len(o1_losses)):
        o1_loss = o1_losses[i].detach()
        o2_loss = o2_losses[i].detach()
        np.testing.assert_array_equal(o1_loss, o2_loss)

    mlp6 = MLP()
    mlp6.set_state_dict(state_dict)
    mlp6.set_state_dict(state_dict)
    o2_losses = train_mlp(
        mlp6,
        use_sharding_stage1=False,
        use_pure_bf16=True,
    )
    for i in range(len(o1_losses)):
        o1_loss = o1_losses[i].detach()
        o2_loss = o2_losses[i].detach()
        np.testing.assert_array_equal(o1_loss, o2_loss)

    # test bf16 + accumlate_grad
    mlp7 = MLP()
    mlp8 = MLP()
    mlp7.set_state_dict(state_dict)
    mlp8.set_state_dict(state_dict)
    o1_losses = train_mlp(
        mlp7,
        use_sharding_stage1=True,
        accumulate_grad=True,
        use_pure_bf16=True,
        test_dp=True,
    )
    o2_losses = train_mlp(
        mlp8,
        use_sharding_stage1=True,
        accumulate_grad=True,
        use_pure_bf16=True,
    )
    for i in range(len(o1_losses)):
        o1_loss = o1_losses[i].detach()
        o2_loss = o2_losses[i].detach()
        np.testing.assert_array_equal(o1_loss, o2_loss)

    mlp9 = MLP()
    mlp9.set_state_dict(state_dict)
    o2_losses = train_mlp(
        mlp9,
        use_sharding_stage1=False,
        accumulate_grad=True,
        use_pure_bf16=True,
    )
    for i in range(len(o1_losses)):
        o1_loss = o1_losses[i].detach()
        o2_loss = o2_losses[i].detach()
        np.testing.assert_array_equal(o1_loss, o2_loss)

    return


if __name__ == '__main__':
    test_stage1_bf16_dp()

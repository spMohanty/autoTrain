from copy import deepcopy
from typing import List, Iterable

import torch
import torchvision
from torch.utils.data import Dataset, DataLoader

__all__ = ["Task"]


class Batch:
    def __init__(self, x, y):
        self._x = x
        self._y = y


class Task:
    """
    Example implementation of an optimization task

    Interface:
        The following methods are exposed to the challenge participants:
            - `train_iterator`: returns an iterator of `Batch`es from the training set,
            - `batchLoss`: evaluate the function value of a `Batch`,
            - `batchLossAndGradient`: evaluate the function value of a `Batch` and compute the gradients,
            - `test`: compute the test loss of the model on the test set.
        The following attributes are exposed to the challenge participants:
            - `default_batch_size`
            - `target_test_loss`

        See documentation below for more information.

    Example:
        See train_sgd.py for an example
    """

    default_batch_size = 128
    target_test_loss = 0.6
    _time_to_converge = 10000  # seconds

    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._num_workers = 2
        self._test_batch_size = 100

        torch.random.manual_seed(42)
        self._model = ResNet(BasicBlock, [2, 2, 2, 2])
        self._model.to(self.device)
        self._model.train()

        self.state = [parameter.data for parameter in self._model.parameters()]

        self._train_set, self._test_set = _get_dataset()

        self._test_loader = DataLoader(
            self._test_set,
            batch_size=self._test_batch_size,
            shuffle=False,
            num_workers=self._num_workers,
        )

        self._criterion = torch.nn.CrossEntropyLoss()

    def train_iterator(self, batch_size: int, shuffle: bool) -> Iterable[Batch]:
        """Create a dataloader serving `Batch`es from the training dataset.

        Example:
            >>> for batch in task.train_iterator(batch_size=32, shuffle=True):
            ...     batch_loss, gradients = task.batchLossAndGradient(batch)
        """
        train_loader = DataLoader(
            self._train_set, batch_size=batch_size, shuffle=shuffle, num_workers=self._num_workers
        )

        def batcher(datum):
            x, y = datum
            x = x.to(self.device)
            y = y.to(self.device)
            return Batch(x, y)

        class _Iterable:
            def __init__(self):
                pass

            def __len__(self):
                # useful to specify the length for tqdm
                return len(train_loader)

            def __iter__(self):
                return map(batcher, iter(train_loader))

        return _Iterable()

    def batchLoss(self, batch: Batch) -> float:
        """
        Evaluate the loss on a batch.
        If the model has batch normalization or dropout, this will run in training mode.
        """
        return self._criterion(self._model(batch._x), batch._y).item()

    def batchLossAndGradient(self, batch: Batch) -> (float, List[torch.Tensor]):
        """
        Evaluate the loss and its gradients on a batch.
        If the model has batch normalization or dropout, this will run in training mode.

        Returns:
            - function value (float)
            - gradients (list of tensors in the same order as task.state())
        """
        self._zero_grad()
        f = self._criterion(self._model(batch._x), batch._y)
        f.backward()
        df = [parameter.grad.data for parameter in self._model.parameters()]
        return f.item(), df

    def test(self, state):
        """
        Compute the average loss on the test set.
        The task is completed as soon as the output is below self.target_test_loss.
        If the model has batch normalization or dropout, this will run in eval mode.
        """
        test_model = self._build_test_model(state)
        mean_f = MeanAccumulator()
        for x, y in self._test_loader:
            x = x.to(self.device)
            y = y.to(self.device)
            with torch.no_grad():
                f = self._criterion(test_model(x), y)
            mean_f.add(f)
        if mean_f.value() < self.target_test_loss:
            raise Done(mean_f.value())
        return mean_f.value()

    def _build_test_model(self, state):
        test_model = deepcopy(self._model)
        test_model.eval()
        for param, new_value in zip(test_model.parameters(), state):
            param.data = new_value.data
        return test_model

    def _zero_grad(self):
        for param in self._model.parameters():
            param.grad = None


class Done(Exception):
    pass


def _get_dataset(data_root="./data"):
    """Create train and test datasets"""
    dataset = torchvision.datasets.CIFAR10

    data_mean = (0.4914, 0.4822, 0.4465)
    data_stddev = (0.2023, 0.1994, 0.2010)

    transform_train = torchvision.transforms.Compose(
        [
            torchvision.transforms.RandomCrop(32, padding=4),
            torchvision.transforms.RandomHorizontalFlip(),
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Normalize(data_mean, data_stddev),
        ]
    )

    transform_test = torchvision.transforms.Compose(
        [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Normalize(data_mean, data_stddev),
        ]
    )

    training_set = dataset(root=data_root, train=True, download=True, transform=transform_train)
    test_set = dataset(root=data_root, train=False, download=True, transform=transform_test)

    return training_set, test_set


class ResNet(torch.nn.Module):
    """
    Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun
    Deep Residual Learning for Image Recognition. arXiv:1512.03385
    Source: github.com/kuangliu/pytorch-cifar
    """

    def __init__(self, block, num_blocks, num_classes=10, use_batchnorm=True):
        super(ResNet, self).__init__()
        self.in_planes = 64
        self.use_batchnorm = use_batchnorm
        self.conv1 = torch.nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn1 = torch.nn.BatchNorm2d(64) if use_batchnorm else torch.nn.Sequential()
        self.layer1 = self._make_layer(block, 64, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, 128, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, 256, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block, 512, num_blocks[3], stride=2)
        self.linear = torch.nn.Linear(512 * block.expansion, num_classes)

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride, self.use_batchnorm))
            self.in_planes = planes * block.expansion
        return torch.nn.Sequential(*layers)

    def forward(self, x):
        out = torch.nn.functional.relu(self.bn1(self.conv1(x)))
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        out = torch.nn.functional.avg_pool2d(out, 4)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out


class BasicBlock(torch.nn.Module):
    """
    Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun
    Deep Residual Learning for Image Recognition. arXiv:1512.03385
    Source: github.com/kuangliu/pytorch-cifar
    """

    expansion = 1

    def __init__(self, in_planes, planes, stride=1, use_batchnorm=True):
        super(BasicBlock, self).__init__()
        self.conv1 = torch.nn.Conv2d(
            in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False
        )
        self.bn1 = torch.nn.BatchNorm2d(planes)
        self.conv2 = torch.nn.Conv2d(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = torch.nn.BatchNorm2d(planes)

        if not use_batchnorm:
            self.bn1 = self.bn2 = torch.nn.Sequential()

        self.shortcut = torch.nn.Sequential()
        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = torch.nn.Sequential(
                torch.nn.Conv2d(
                    in_planes, self.expansion * planes, kernel_size=1, stride=stride, bias=False
                ),
                torch.nn.BatchNorm2d(self.expansion * planes)
                if use_batchnorm
                else torch.nn.Sequential(),
            )

    def forward(self, x):
        out = torch.nn.functional.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = torch.nn.functional.relu(out)
        return out


class Bottleneck(torch.nn.Module):
    """
    Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun
    Deep Residual Learning for Image Recognition. arXiv:1512.03385
    Source: github.com/kuangliu/pytorch-cifar
    """

    expansion = 4

    def __init__(self, in_planes, planes, stride=1, use_batchnorm=True):
        super(Bottleneck, self).__init__()
        self.conv1 = torch.nn.Conv2d(in_planes, planes, kernel_size=1, bias=False)
        self.bn1 = torch.nn.BatchNorm2d(planes)
        self.conv2 = torch.nn.Conv2d(
            planes, planes, kernel_size=3, stride=stride, padding=1, bias=False
        )
        self.bn2 = torch.nn.BatchNorm2d(planes)
        self.conv3 = torch.nn.Conv2d(planes, self.expansion * planes, kernel_size=1, bias=False)
        self.bn3 = torch.nn.BatchNorm2d(self.expansion * planes)

        if not use_batchnorm:
            self.bn1 = self.bn2 = self.bn3 = torch.nn.Sequential()

        self.shortcut = torch.nn.Sequential()
        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = torch.nn.Sequential(
                torch.nn.Conv2d(
                    in_planes, self.expansion * planes, kernel_size=1, stride=stride, bias=False
                ),
                torch.nn.BatchNorm2d(self.expansion * planes)
                if use_batchnorm
                else torch.nn.Sequential(),
            )

    def forward(self, x):
        out = torch.nn.functional.relu(self.bn1(self.conv1(x)))
        out = torch.nn.functional.relu(self.bn2(self.conv2(out)))
        out = self.bn3(self.conv3(out))
        out += self.shortcut(x)
        out = torch.nn.functional.relu(out)
        return out


class MeanAccumulator:
    """
    Running average of the values that are 'add'ed
    """

    def __init__(self, update_weight=1):
        """
        :param update_weight: 1 for normal, 2 for t-average
        """
        self.average = None
        self.counter = 0
        self.update_weight = update_weight

    def add(self, value, weight=1):
        """Add a value to the accumulator"""
        self.counter += weight
        if self.average is None:
            self.average = deepcopy(value)
        else:
            delta = value - self.average
            self.average += (
                delta * self.update_weight * weight / (self.counter + self.update_weight - 1)
            )
            if isinstance(self.average, torch.Tensor):
                self.average.detach()

    def value(self):
        """Access the current running average"""
        return self.average

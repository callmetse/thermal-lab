"""Small temporal conv prototype. Class labels describe events, not metabolic truth."""
import torch
from torch import nn


class ActionTCN(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(54, 64, 5, padding=2), nn.BatchNorm1d(64), nn.ReLU(),
            nn.Conv1d(64, 64, 5, padding=4, dilation=2), nn.BatchNorm1d(64), nn.ReLU(),
            nn.Dropout(0.2),
            nn.Conv1d(64, 64, 5, padding=8, dilation=4), nn.BatchNorm1d(64), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1), nn.Flatten(), nn.Linear(64, 4))

    def forward(self, x):
        return self.net(x)

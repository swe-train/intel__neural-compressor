# -*- coding: utf-8 -*-
# Copyright (c) 2022 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The TuningDetails class."""
import json

from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.orm import session
from sqlalchemy.sql import func

from neural_compressor.ux.components.db_manager.db_manager import Base


class TuningDetails(Base):
    """INC Bench tuning details' table representation."""

    __tablename__ = "tuning_details"

    id = Column(Integer, primary_key=True, index=True, unique=True)
    strategy = Column(String(50))
    accuracy_criterion_type = Column(String(50), default="relative")
    accuracy_criterion_threshold = Column(Float, default=0.1)
    objective = Column(String(50))
    exit_policy = Column(String)
    random_seed = Column(Integer)
    created_at = Column(DateTime, nullable=False, default=func.now())
    modified_at = Column(DateTime, nullable=True, onupdate=func.now())

    @staticmethod
    def add(
        db_session: session.Session,
        strategy: str,
        accuracy_criterion_type: str,
        accuracy_criterion_threshold: float,
        objective: str,
        exit_policy: dict,
        random_seed: int,
    ) -> int:
        """
        Add optimization to database.

        returns id of added optimization
        """
        new_tuning_details = TuningDetails(
            strategy=strategy,
            accuracy_criterion_type=accuracy_criterion_type,
            accuracy_criterion_threshold=accuracy_criterion_threshold,
            objective=objective,
            exit_policy=json.dumps(exit_policy),
            random_seed=random_seed,
        )
        db_session.add(new_tuning_details)
        db_session.flush()

        return int(new_tuning_details.id)

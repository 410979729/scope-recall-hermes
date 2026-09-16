"""Transactional storage for the C3 candidate processing service.

``tx.candidates`` is one object assembled from three responsibilities, each in
its own module so it can be read on its own:

* ``candidate_intake``      -- registering claim versions, indexing them for
                                source triggers and queueing evaluations;
* ``candidate_evaluations`` -- the worker's bookkeeping around one evaluation
                                attempt, and fencing on deletion;
* ``candidate_sweeps``      -- bounded maintenance passes and the counts the
                                doctor reports.

``candidate_tables`` underneath holds the SQL they share.
"""
from __future__ import annotations

from .candidate_evaluations import CandidateEvaluations
from .candidate_sweeps import CandidateSweeps


class CandidateLifecycle(CandidateSweeps, CandidateEvaluations):
    """Candidate metadata borrowed from the existing owning transaction."""


__all__ = ["CandidateLifecycle"]

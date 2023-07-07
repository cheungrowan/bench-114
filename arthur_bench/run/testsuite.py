import logging
import json
import pandas as pd
from typing import List, Optional, Union
from pathlib import Path

from arthur_bench.scoring import ScoringMethod, load_scoring_method
from arthur_bench.models.models import TestSuiteRequest, ScoringMethod as ScoringEnum, TestCaseOutput
from arthur_bench.client.exceptions import UserValueError, ArthurInternalError
from arthur_bench.run.testrun import TestRun
from arthur_bench.run.utils import _create_test_suite_dir, _initialize_metadata, _test_suite_dir, \
	_create_run_dir, _clean_up_run, _load_suite_from_args, _load_run_data_from_args, _get_suite_if_exists, _get_scoring_method


logger = logging.getLogger(__name__)


DEFAULT_BATCH_SIZE = 32


class TestSuite:
	"""
		Reusable pipeline for running a test suite built from reference_data and evaluated using metric

		:param name: name of the test suite
		:param scoring_method: scoring method to use to evaluate the results of a test run
		:param description: short description of the task tested by this suite
		:param reference_data: dataframe of prompts and reference outputs
		:param reference_data_path: filepath to csv of prompts and reference outputs,
			required if not specifying reference_data
		:param input_column: the column of reference_data containing prompts, defaults to 'prompt'
		:param reference_column: the column of reference_data containing reference outputs, defaults to 'reference'
		:param input_text_list: list of strings of input texts that can be provided instead of dataframe columns
		:param reference_output_list: list of strings of reference outputs that can be provided instead of dataframe columns
	"""
	def __init__(
			self,
			name: str,
			scoring_method: Union[ScoringEnum, str],
			description: Optional[str] = None,
			reference_data: Optional[pd.DataFrame] = None,
			reference_data_path: Optional[str] = None,
			input_column: str = "input",
			reference_column: str = "reference_output", 
			input_text_list: Optional[List[str]] = None,
			reference_output_list: Optional[List[str]] = None
	):
		self.id = None
		self.suite: TestSuiteRequest = _get_suite_if_exists(name) # type: ignore

		if self.suite is None:
			scoring_method = _get_scoring_method(scoring_method=scoring_method)
			if scoring_method == ScoringEnum.QACorrectness:
				reference_column = None
			cases = _load_suite_from_args(
				reference_data=reference_data,
				reference_data_path=reference_data_path,
				input_column=input_column,
				reference_column=reference_column,
				input_text_list=input_text_list,
				reference_output_list=reference_output_list
			)
			self.suite = TestSuiteRequest(
				name=name,
				scoring_method=scoring_method,
				description=description,
				test_cases=cases,
				**_initialize_metadata()
			)
			self._test_suite_dir: Path = _create_test_suite_dir(name)

		else:
			logger.info(f"Found existing test suite with name {name}. Using existing suite")
			self._test_suite_dir = _test_suite_dir(name)

	def run(
			self,
			run_name: str,
			candidate_data: Optional[pd.DataFrame] = None,
			candidate_data_path: Optional[str] = None,
			candidate_column: str = "candidate_output",
			candidate_output_list: Optional[List[str]] = None,
			context_column: Optional[str] = None,
			context_list: Optional[List[str]] = None,
			save: bool = True,
			batch_size: int = DEFAULT_BATCH_SIZE,
			model_name: Optional[str] = None,
			model_version: Optional[str] = None,
			foundation_model: Optional[str] = None,
			prompt_template: Optional[str] = None

	) -> TestRun:
		"""
		Score a test run on candidate outputs.

		:param run_name: name for the test run
		:param candidate_data: dataframe of candidate responses to test prompts
		:param candidate_data_path: filepath to csv containing candidate responses to test prompts
		:param candidate_column: the column of candidate data containing candidate responses,
			defaults to 'candidate_output'
		:param candidate_output_list: list of strings of candidate outputs that can be provided instead of dataframe
		:param context_column: the column of reference_data containing supporting context for answering Question & Answering tasks
		:param context_list: list of strings containing supporting context for answering question and answering tasks
		:param save: whether to save the run results to file
		:param batch_size: the batch_size to use when computing scores
		:param model_name: model name for model used to generate outputs
		:param model_version: model version of model used to generate outputs
		:param foundation_model: foundation model name used to generate outputs
		:param prompt_template: prompt template name used to generate outputs
		:returns: TestRun object containing scored outputs
		"""
		candidate_output_list, context_list = _load_run_data_from_args(
			candidate_data=candidate_data,
			candidate_data_path=candidate_data_path,
			candidate_column=candidate_column,
			candidate_output_list=candidate_output_list,
			context_column=context_column,
			context_list=context_list,
		)

		if len(candidate_output_list) != len(self.suite.test_cases):
			raise UserValueError(
				f"candidate data has {len(candidate_output_list)} tests but expected {len(self.suite.test_cases)} tests")
			

		scoring_method: ScoringMethod = load_scoring_method(self.suite.scoring_method)

		run_dir = None
		if save:
			run_dir = _create_run_dir(self.suite.name, run_name)

		try:
			all_scores = []
			for i in range(0, len(self.suite.test_cases), batch_size):
				# TODO: make suite iterable: https://arthurai.atlassian.net/browse/LLM-250
				batch = [(case.input, case.reference_output) for case in self.suite.test_cases[i:i+batch_size]]
				input_batch, ref_batch = zip(*batch)

				if context_list is not None:  
					scores = scoring_method.run_batch(
						list(ref_batch),
						candidate_output_list[i:i+batch_size],
						list(input_batch), 
						context_list[i:i+batch_size]
					)
				else: 
					scores = scoring_method.run_batch(
						list(ref_batch),
						candidate_output_list[i:i+batch_size],
						list(input_batch)
					)

				all_scores.extend(scores)
		except Exception as e:
			logger.error(f"failed to create run: {e}")
			if run_dir:
				_clean_up_run(run_dir=run_dir)
			raise ArthurInternalError(f"failed to create run {run_name}") from e
		
		test_case_outputs = [TestCaseOutput(output=output, score=score) for output, score in zip(candidate_output_list, all_scores)]
		
		run = TestRun(
			name=run_name,
			test_case_outputs=test_case_outputs,
			model_name=model_name,
			model_version=model_version,
			foundation_model=foundation_model,
			prompt_template=prompt_template,
			run_dir=run_dir,
			**_initialize_metadata()
		)

		if save:
			self.save()
			run.save()

		return run

	def save(self):
		"""Save a test suite to local file system."""
		suite_file = self._test_suite_dir / "suite.json"
		suite_file.write_text(self.suite.json())
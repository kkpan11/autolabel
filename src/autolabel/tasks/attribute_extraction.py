import copy
import json
import logging
import pickle
import re
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple, Union

import json5
from langchain.prompts.prompt import PromptTemplate
from langchain.schema import ChatGeneration, Generation

from autolabel.configs import AutolabelConfig
from autolabel.metrics import (
    AccuracyMetric,
    AUROCMetric,
    BaseMetric,
    CompletionRateMetric,
    SupportMetric,
)
from autolabel.schema import (
    ErrorType,
    LabelingError,
    LLMAnnotation,
    MetricResult,
    TaskType,
)
from autolabel.utils import get_format_variables

from .base import BaseTask

logger = logging.getLogger(__name__)


class AttributeExtractionTask(BaseTask):
    NULL_LABEL = {}
    DEFAULT_TASK_GUIDELINES = "You are an expert at extracting attributes from text. Given a piece of text, extract the required attributes."
    DEFAULT_OUTPUT_GUIDELINES = "You will return the extracted attributes as a json with the following keys:\n{attribute_json}. \n Do not include keys in the final JSON that don't have any valid value extracted."
    LABEL_FORMAT_IN_EXPLANATION = (
        " The explanation should end with - 'so, the answer is <label>.'"
    )
    EXCLUDE_LABEL_IN_EXPLANATION = " Do not repeat the output of the task - simply provide an explanation for the provided output. The provided label was generated by you in a previous step and your job now is to only provided an explanation for the output. Your job is not verify the output but instead explain why it might have been generated, even if it is incorrect. If you think the provided output is incorrect, give an explanation of why it might have been generated anyway but don't say that the output may be incorrect or incorrectly generated.'"
    GENERATE_EXPLANATION_PROMPT = "You are an expert at providing a well reasoned explanation for the output of a given task. \n\nBEGIN TASK DESCRIPTION\n{task_guidelines}\nEND TASK DESCRIPTION\nYou will be given an input example and the output for one of the attributes. Your job is to provide an explanation for why the output for that attribute is correct for the task above.\nYour explanation should be at most two sentences.{label_format}\n{labeled_example}\nCurrent Attribute:{attribute}.\nExplanation: "
    OUTPUT_DICT_KEY = "output_dict"

    def __init__(self, config: AutolabelConfig) -> None:
        super().__init__(config)

        self.metrics = [
            SupportMetric(),
            CompletionRateMetric(),
            AccuracyMetric(),
        ]

        if self.config.confidence():
            self.metrics.append(AUROCMetric())

    def _construct_attribute_json(
        self,
        selected_labels_map: Dict[str, List[str]] = None,
        selected_labels_desc_map: Dict[str, Dict[str, str]] = None,
    ) -> Tuple[str, Dict]:
        """
        This function is used to construct the attribute json string for the output guidelines.

        Args:
            attributes (List[Dict]): A list of dictionaries containing the output attributes.

        Returns:
            str: A string containing the output attributes.

        """
        output_json, output_schema = (
            {},
            {
                "title": "AnswerFormat",
                "description": "Answer to the provided prompt.",
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
                "definitions": {},
            },
        )
        for attribute_dict in self.config.attributes():
            if "name" not in attribute_dict or "description" not in attribute_dict:
                raise ValueError(
                    "Attribute dictionary must contain 'name' and 'description' keys",
                )

            attribute_desc = attribute_dict["description"]
            attribute_name = attribute_dict["name"]

            if (
                attribute_dict.get(
                    "task_type",
                    "",
                )
                == TaskType.MULTILABEL_CLASSIFICATION
            ):
                attribute_desc += " The output format should be all the labels separated by semicolons. For example: label1;label2;label3"

            if len(attribute_dict.get("options", [])) > 0 or (
                selected_labels_map
                and len(selected_labels_map.get(attribute_name, [])) > 0
            ):
                attribute_options = attribute_dict.get("options", [])
                if selected_labels_map and attribute_name in selected_labels_map:
                    attribute_options = selected_labels_map[attribute_name]
                attribute_desc += f"\nOptions:\n{','.join(attribute_options)}"

                attribute_options_desc = attribute_dict.get("options_desc", {})
                if (
                    selected_labels_desc_map
                    and attribute_name in selected_labels_desc_map
                ):
                    attribute_options_desc = selected_labels_desc_map[attribute_name]
                attribute_options_desc = {
                    k: v for k, v in attribute_options_desc.items() if v is not None
                }
                if attribute_options_desc:
                    attribute_desc += "\nDescription for each option:"
                    for k, v in attribute_options_desc.items():
                        attribute_desc += f"\n{k}: {v}"

            output_json[attribute_name] = attribute_desc

            if (
                "schema" in attribute_dict
                and attribute_dict["schema"] is not None
                and len(attribute_dict["schema"]) > 0
            ):
                curr_property = {"$ref": "#/definitions/" + attribute_name}
                output_schema["definitions"][attribute_name] = json5.loads(
                    attribute_dict["schema"],
                )
            else:
                curr_property = {"title": attribute_dict["name"], "type": "string"}
                if "options" in attribute_dict and len(attribute_dict["options"]) < 500:
                    if attribute_dict.get("task_type", "") == TaskType.CLASSIFICATION:
                        curr_property = {"$ref": "#/definitions/" + attribute_name}
                        output_schema["definitions"][attribute_name] = {
                            "title": attribute_name,
                            "description": "An enumeration.",
                            "enum": attribute_options,
                        }

            output_schema["properties"][attribute_name] = copy.deepcopy(curr_property)
            output_schema["required"].append(attribute_name)
        return json.dumps(output_json, indent=4), output_schema

    def _generate_output_dict(self, input: Dict) -> Optional[str]:
        """
        Generate the output dictionary from the input

        Args:
            input (Dict): The input dictionary

        Returns:
            Dict: The output dictionary

        """
        output_dict = {}
        for attribute in self.config.attributes():
            attribute_name = attribute["name"]
            output_dict[attribute_name] = input.get(attribute_name, "")
        if not self._validate_output_dict(output_dict):
            logger.warning(
                f"Generated output dict: {output_dict} does not contain all the expected output attributes. Skipping example.",
            )
            return None
        return json.dumps(output_dict)

    def _validate_output_dict(self, output_dict: Dict) -> bool:
        """
        Validate the output dictionary

        Args:
            output_dict (Dict): The output dictionary

        Returns:
            bool: True if the output dictionary is valid, False otherwise

        """
        for attribute in self.config.attributes():
            attribute_name = attribute.get("name")
            attribute_value = output_dict.get(attribute_name)
            if attribute_value is None or len(str(attribute_value)) == 0:
                return False
        return True

    def construct_prompt(
        self,
        input: Dict,
        examples: List,
        prompt_template_override: Optional[PromptTemplate] = None,
        output_guidelines_override: Optional[str] = None,
        max_input_tokens: Optional[int] = None,
        get_num_tokens: Optional[Callable] = None,
        selected_labels_map: Dict[str, List[str]] = None,
        selected_labels_desc_map: Dict[str, Dict[str, str]] = None,
        **kwargs,
    ) -> Tuple[str, str]:
        fmt_task_guidelines = self.task_guidelines

        # add additional labels to the selected_labels_map for attributes
        # if they are present in the few shot examples
        if self._is_few_shot_mode() and selected_labels_map:
            for eg in examples:
                for attribute in self.config.attributes():
                    attribute_name = attribute["name"]
                    if attribute_name not in selected_labels_map:
                        continue

                    label = eg.get(attribute_name)
                    if not label:
                        continue

                    attr_type = attribute.get("task_type")
                    if attr_type == TaskType.MULTILABEL_CLASSIFICATION:
                        labels = label.split(self.config.label_separator())
                    else:
                        labels = [label]

                    for l in labels:
                        if l not in selected_labels_map[attribute_name]:
                            selected_labels_map[attribute_name].append(l)

        attribute_json, output_schema = self._construct_attribute_json(
            selected_labels_map=selected_labels_map,
            selected_labels_desc_map=selected_labels_desc_map,
        )
        output_guidelines = (
            self.output_guidelines
            if output_guidelines_override is None
            else output_guidelines_override
        )
        fmt_output_guidelines = output_guidelines.format(attribute_json=attribute_json)

        # prepare seed examples
        example_template = self.config.example_template()
        fmt_examples = []
        for eg in examples:
            if self.OUTPUT_DICT_KEY not in eg:
                output_dict = self._generate_output_dict(eg)
                if output_dict is None:
                    continue
                eg.update({self.OUTPUT_DICT_KEY: output_dict})
            fmt_examples.append(example_template.format_map(defaultdict(str, eg)))

        input[self.OUTPUT_DICT_KEY] = ""

        # check if all mapped keys in input are in the example template
        try:
            current_example = example_template.format(**input)
        except KeyError as e:
            try:
                current_example = example_template.format_map(defaultdict(str, input))
                logger.warning(
                    f'\n\nKey {e} in the "example_template" in the given config'
                    f"\n\n{example_template}\n\nis not present in the datsaset columns - {input.keys()}.\n\n"
                    f"Input - {input}\n\n"
                    "Continuing with the prompt as {current_example}",
                )
            except AttributeError as e:
                for key in input:
                    if input[key] is not None:
                        example_template = example_template.replace(
                            f"{{{key}}}",
                            input[key],
                        )
                current_example = example_template

        # populate the current example in the prompt
        prompt_template = (
            self.prompt_template
            if prompt_template_override is None
            else prompt_template_override
        )
        if self._is_few_shot_mode():
            curr_text_prompt = self.trim_prompt(
                prompt_template,
                task_guidelines=fmt_task_guidelines,
                output_guidelines=fmt_output_guidelines,
                seed_examples="\n\n".join(fmt_examples),
                current_example=current_example,
                max_input_tokens=max_input_tokens,
                get_num_tokens=get_num_tokens,
            )
        else:
            curr_text_prompt = self.trim_prompt(
                prompt_template,
                task_guidelines=fmt_task_guidelines,
                output_guidelines=fmt_output_guidelines,
                current_example=current_example,
                max_input_tokens=max_input_tokens,
                get_num_tokens=get_num_tokens,
            )
        if self.image_cols:
            prompt_dict = {"text": curr_text_prompt}
            for col in self.image_cols:
                if (
                    col in self.input_cols
                    and input.get(col) is not None
                    and len(input.get(col)) > 0
                ):
                    prompt_dict[col] = input[col]
                prompt_dict[col] = input[col]
            return json.dumps(prompt_dict), output_schema
        return curr_text_prompt, output_schema

    def get_explanation_prompt(self, example: Dict, include_label=True) -> str:
        pt = PromptTemplate(
            input_variables=get_format_variables(self.GENERATE_EXPLANATION_PROMPT),
            template=self.GENERATE_EXPLANATION_PROMPT,
        )

        fmt_task_guidelines = self.task_guidelines
        # prepare labeled example
        example_template = self.config.example_template()
        fmt_example = example_template.format_map(defaultdict(str, example))
        return pt.format(
            task_guidelines=fmt_task_guidelines,
            label_format=(
                self.LABEL_FORMAT_IN_EXPLANATION
                if include_label
                else self.EXCLUDE_LABEL_IN_EXPLANATION
            ),
            labeled_example=fmt_example,
            attribute=example[self.OUTPUT_DICT_KEY],
        )

    def get_generate_dataset_prompt(
        self,
        label: str,
        num_rows: int,
        guidelines: str = None,
    ) -> str:
        raise NotImplementedError("Dataset generation not implemented for this task")

    def parse_llm_response(
        self,
        response: Union[Generation, ChatGeneration],
        curr_sample: Dict,
        prompt: str,
        selected_labels_map: Dict[str, List[str]] = None,
    ) -> LLMAnnotation:
        successfully_labeled = False
        error = None
        try:
            completion_text = response.text

            # Remove markdown formatting from the completion text
            completion_text = completion_text.lstrip("```json")
            completion_text = completion_text.rstrip("```")
            llm_label = {}
            for k, v in json5.loads(completion_text).items():
                if isinstance(v, list) or isinstance(v, dict):
                    llm_label[k] = v
                else:
                    llm_label[k] = str(v)
            successfully_labeled = True
        except Exception as e:
            logger.info(
                f"Error parsing LLM response: {response.text}, Error: {e}. Now searching for valid JSON in response",
            )
            try:
                json_start, json_end = response.text.find("{"), response.text.rfind("}")
                json_str = re.sub(
                    r'"[^"]*"',
                    lambda m: m.group().replace("\n", "\\n"),
                    response.text[json_start : json_end + 1],
                )
                llm_label = {}
                for k, v in json5.loads(
                    json_str,
                ).items():
                    if isinstance(v, list) or isinstance(v, dict):
                        llm_label[k] = v
                    else:
                        llm_label[k] = str(v)
                successfully_labeled = True
            except Exception as e:
                logger.error(f"Error parsing LLM response: {response.text}, Error: {e}")
                llm_label = self.NULL_LABEL
                error = LabelingError(
                    error_type=ErrorType.INVALID_LLM_RESPONSE_ERROR,
                    error_message=str(e),
                )

        if successfully_labeled:
            for attribute in self.config.attributes():
                attr_options = attribute.get("options")
                if selected_labels_map and attribute["name"] in selected_labels_map:
                    attr_options = selected_labels_map[attribute["name"]]
                attr_type = attribute.get("task_type")
                if attr_options is not None and len(attr_options) > 0:
                    attr_label = str(llm_label.get(attribute["name"]))
                    if attr_type == TaskType.CLASSIFICATION:
                        if attr_label is not None and attr_label not in attr_options:
                            logger.warning(
                                f"Attribute {attr_label} from the LLM response {llm_label} is not in the labels list",
                            )
                            llm_label.pop(attribute["name"], None)
                    elif attr_type == TaskType.MULTILABEL_CLASSIFICATION:
                        original_attr_labels = attr_label.split(
                            self.config.label_separator(),
                        )
                        filtered_attr_labels = list(
                            filter(
                                lambda x: x.strip() in attr_options,
                                original_attr_labels,
                            ),
                        )
                        llm_label[
                            attribute["name"]
                        ] = self.config.label_separator().join(filtered_attr_labels)
                        if len(filtered_attr_labels) != len(original_attr_labels):
                            logger.warning(
                                f"Attribute {attr_label} from the LLM response {llm_label} is not in the labels list. Filtered list: {filtered_attr_labels}",
                            )
                        if len(filtered_attr_labels) == 0:
                            llm_label.pop(attribute["name"], None)
        return LLMAnnotation(
            curr_sample=pickle.dumps(curr_sample),
            successfully_labeled=successfully_labeled,
            label=llm_label,
            generation_info=response.generation_info,
            raw_response=response.text,
            prompt=prompt,
            error=error,
            selected_labels_map=selected_labels_map,
        )

    def eval(
        self,
        llm_labels: List[LLMAnnotation],
        gt_labels: List[str],
        additional_metrics: List[BaseMetric] = [],
    ) -> List[MetricResult]:
        """Evaluate the LLM generated labels by comparing them against ground truth"""
        # Convert the llm labels into a mapping from
        # name -> List[LLMAnnotation]
        llm_labels_dict = defaultdict(list)
        for llm_label in llm_labels:
            for attribute, value in llm_label.label.items():
                llm_labels_dict[attribute].append(
                    LLMAnnotation(
                        successfully_labeled=llm_label.successfully_labeled,
                        label=value,
                        raw_response=llm_label.raw_response,
                        curr_sample=llm_label.curr_sample,
                        prompt=llm_label.prompt,
                        error=llm_label.error,
                        confidence_score=(
                            llm_label.confidence_score[attribute]
                            if llm_label.confidence_score
                            else 0
                        ),
                    ),
                )

        eval_metrics = []
        macro_metrics = {}

        for attribute in llm_labels_dict.keys():
            for metric in self.metrics + additional_metrics:
                if attribute not in gt_labels or gt_labels[attribute] is None:
                    continue

                computed_metrics = metric.compute(
                    llm_labels_dict[attribute],
                    gt_labels[attribute],
                )
                for m in computed_metrics:
                    eval_metrics.append(
                        MetricResult(
                            name=f"{attribute}:{m.name}",
                            value=m.value,
                        ),
                    )
                    if m.name not in macro_metrics:
                        macro_metrics[m.name] = []
                    macro_metrics[m.name].append(m.value)

        for key in macro_metrics:
            eval_metrics.append(
                MetricResult(
                    name=f"Macro:{key}",
                    value=sum(macro_metrics[key]) / len(macro_metrics[key]),
                ),
            )

        return eval_metrics

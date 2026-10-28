"""Run reproducible local metrics: python -m evaluation.run"""
import json
from pathlib import Path
from .metrics import evaluate_dataset
BASE=Path(__file__).parent
def main():
    dataset=json.loads((BASE/"dataset_v1.json").read_text(encoding="utf-8-sig"))
    result=evaluate_dataset(dataset)
    out=BASE/"results_latest.json"
    out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({"result_file":str(out),"dataset_version":result["dataset_version"],"case_count":result["case_count"],"metrics":result["metrics"]},indent=2))
if __name__=="__main__": main()

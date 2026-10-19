# Alex Rivera: fact bank

Everything in this file is true and may be used in a tailored resume. A number that is neither here nor in the
base resume fails the honesty check, so every metric Alex might want is written down once, with its context.

## Lumen Freight, USA (Mar 2023 to present), Senior Machine Learning Engineer
- Demand forecasting service (PyTorch, Airflow, AWS) predicts load volume for 1,200 routes; empty truck miles
  down 14%. Served behind a FastAPI REST endpoint; p95 latency 120 ms.
- Retrieval tool for dispatchers (LangChain, pgvector, Claude): answers questions from 40,000 shipping documents.
  Evaluation set of 600 questions runs on every release; answer accuracy went from 71% to 89%.
- Moved training to SageMaker with MLflow and Docker: retraining from two days to five hours; weekly releases
  instead of monthly. GPU training on g5 instances.
- Drift monitoring and alerts for 9 production models on Kubernetes (Prometheus, Grafana); caught two broken
  data pipelines before they reached pricing.
- Mentored 3 junior engineers; ran the team's model review every two weeks.

## Brightline Health, USA (Jun 2020 to Feb 2023), Machine Learning Engineer
- XGBoost models flag patients likely to miss follow-up visits: scheduled follow-ups up 11% across 23 clinics in a
  six month pilot. False alarms down 30% after adding visit history features and a calibrated threshold.
- Feature pipeline rebuilt in Python and SQL (dbt models on Snowflake), tested and versioned: nightly run from
  3 hours to 35 minutes.
- A/B tests on appointment reminder timing; wrote the analysis templates the analysts still use.
- Worked under HIPAA rules; de-identified training data for 2 research partners.

## Northwind Analytics, USA (Jul 2018 to May 2020), Data Scientist
- Churn models in scikit-learn for retail clients, with plain-language driver summaries for account managers.
- Time series staffing forecasts in pandas for 150 store locations: overtime hours down 8% in year one.
- Automated weekly client reports in Python and SQL: about 10 hours a week saved.
- Joined point of sale data from 12 retail clients into one warehouse schema (Redshift).
- No LLM or GenAI work here; that started at Lumen Freight.

## Education
- M.S. Computer Science, Cascade State University, Sep 2016 to Jun 2018. Thesis on demand forecasting.

## Writing notes
- First person is fine in the summary, never in bullets.
- Lead bullets with what was built, then the number. Plain verbs: built, shipped, cut, moved, wrote, ran.
- Alex is in Portland, OR and open to remote or hybrid roles on the US West Coast.

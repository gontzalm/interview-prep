import os

import aws_cdk as cdk

from .stack import InterviewPrepStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ["CDK_DEFAULT_ACCOUNT"],
    region=os.environ["CDK_DEFAULT_REGION"],
)

_ = InterviewPrepStack(app, "interview-prep-stack", env=env)
_ = InterviewPrepStack(app, "interview-prep-stack-local", local_dev=True, env=env)

_ = app.synth()

"""Install SDK artifacts outside the gateway tree and invoke only the owned synthetic lab."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import venv

ROOT=Path(__file__).resolve().parents[1]


def check(state,packages,docker_node=False):
    state=state.resolve();packages=packages.resolve()
    lab=json.loads((state/'lab.json').read_text())
    if not lab['project'].startswith('pliqra-accept-'):raise ValueError('Only an owned synthetic lab is supported')
    private=json.loads((state/'.secrets.json').read_text())
    artifacts=[next(packages.glob('pliqra_client-*.whl')),next(packages.glob('eturkok-pliqra-*.tgz'))]
    results=[]
    with tempfile.TemporaryDirectory(prefix='installed-clients-',dir=state) as directory:
        directory=Path(directory)
        venv.create(directory/'python',with_pip=True)
        python=directory/'python/bin/python'
        subprocess.run([str(python),'-m','pip','install','--no-index','--no-deps',str(artifacts[0])],check=True,capture_output=True,text=True)
        shutil.copy2(ROOT/'tests/acceptance/consumer.py',directory/'consumer.py')
        environment={**os.environ,'PLIQRA_URL':'https://127.0.0.1:18091','PLIQRA_API_KEY':private['key'],'PLIQRA_SERVICE':'customer-chat','SSL_CERT_FILE':str(state/'pki/ca.pem')}
        result=subprocess.run([str(python),'-I',str(directory/'consumer.py')],cwd=directory,env=environment,check=True,capture_output=True,text=True)
        results.append(json.loads(result.stdout))
        project=directory/'node';project.mkdir()
        (project/'package.json').write_text('{"private":true,"type":"module"}')
        shutil.copy2(ROOT/'tests/acceptance/consumer.mjs',project/'consumer.mjs')
        if docker_node:
            env_file=directory/'node.env'
            env_file.write_text('PLIQRA_URL=https://gateway-west:8080\nPLIQRA_SERVICE=customer-chat\nPLIQRA_API_KEY='+private['key']+'\nNODE_EXTRA_CA_CERTS=/fixture/ca.pem\n')
            env_file.chmod(0o600)
            command=['docker','run','--rm','--network',lab['project']+'_default','--env-file',str(env_file),'-v',str(project)+':/consumer','-v',str(state/'pki')+':/fixture:ro','-v',str(packages)+':/packages:ro','-w','/consumer','node:24-alpine','sh','-ec','npm install --offline --ignore-scripts --no-audit --no-fund /packages/'+artifacts[1].name+' >&2; node consumer.mjs']
            result=subprocess.run(command,check=True,capture_output=True,text=True)
        else:
            subprocess.run(['npm','install','--offline','--ignore-scripts','--no-audit','--no-fund',str(artifacts[1])],cwd=project,check=True,capture_output=True,text=True)
            environment['NODE_EXTRA_CA_CERTS']=str(state/'pki/ca.pem')
            result=subprocess.run(['node','consumer.mjs'],cwd=project,env=environment,check=True,capture_output=True,text=True)
        results.append(json.loads(result.stdout))
    report={'synthetic':True,'human_feedback_collected':False,'passed':True,'applications':results,'artifacts':[{'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in artifacts]}
    (state/'installed-clients.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state',type=Path)
    parser.add_argument('--packages',type=Path,default=Path('dist/sdk'))
    parser.add_argument('--docker-node',action='store_true',help='Use the official Node image on the isolated lab network')
    args=parser.parse_args()
    check(args.state,args.packages,args.docker_node)

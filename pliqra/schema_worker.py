"""Private validation worker; never opens a workspace database or provider client."""
import json
import sys


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(60,60))
    if sys.platform=='linux':
        resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource
    from .schema_validation import validate_schema_definition
    def no_remote(uri):
        raise NoSuchResource(ref=uri)
    raw=sys.stdin.buffer.read(20_000_001)
    if len(raw)>20_000_000:
        raise ValueError('Validation input is too large')
    request=json.loads(raw)
    schema=validate_schema_definition(request['schema'])
    validator=Draft202012Validator(schema,registry=Registry(retrieve=no_remote))
    sys.stdout.write(json.dumps({'valid':validator.is_valid(request['value'])}))


if __name__=='__main__':
    try:
        main()
    except Exception:
        sys.exit(2)

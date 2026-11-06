import base64
import json
import zlib
from datetime import datetime,timezone,timedelta
from urllib.parse import urlencode,parse_qs,urlsplit

from onelogin.saml2.utils import OneLogin_Saml2_Utils

from pliqra.db import now
from pliqra.security import digest
from tests.test_app import workspace
from tests.test_product import saml_certificate


def setup(workspace):
    client,app,_=workspace
    private,certificate=saml_certificate()
    provider=client.post('/api/v1/identity-providers/saml',json={'name':'Logout IdP','entity_id':'https://idp.test','sso_url':'https://idp.test/login','slo_url':'https://idp.test/logout','certificate':certificate})
    assert provider.status_code==201,provider.text
    record_id=provider.json()['id'];origin='https://pliqra.example.test'
    app.state.context['public_url']=origin
    app.state.store.insert('users',{'id':'federated-user','email':'federated@example.test','name':'Federated','role':'viewer','issuer':record_id,'subject':'federated-subject','active':1,'created':now()})
    app.state.store.insert('sessions',{'token_hash':digest('federated-token'),'user_id':'federated-user','csrf':'federated-csrf','expires':now()+300,'federation_context':json.dumps({'name_id':'federated-subject','name_id_format':'urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified','session_index':'_session'})})
    client.cookies.set('plq_session','federated-token',domain='pliqra.example.test',path='/')
    return client,app,private,record_id,origin


def signed_query(kind,xml,private,relay=None):
    encoded=base64.b64encode(zlib.compress(xml.encode())[2:-4]).decode()
    fields={kind:encoded}
    if relay:fields['RelayState']=relay
    fields['SigAlg']='http://www.w3.org/2001/04/xmldsig-more#rsa-sha256'
    query=urlencode(fields)
    signature=base64.b64encode(OneLogin_Saml2_Utils.sign_binary(query,private)).decode()
    return query+'&'+urlencode({'Signature':signature})


def stamp(offset=0):return (datetime.now(timezone.utc)+timedelta(seconds=offset)).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_signed_idp_logout_rejects_unsigned_stale_and_replayed_messages(workspace):
    client,app,private,provider,origin=setup(workspace)
    destination=origin+f'/auth/saml/{provider}/sls'
    template='<samlp:LogoutRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_logout_idp" Version="2.0" IssueInstant="{stamp}" Destination="{destination}"><saml:Issuer>https://idp.test</saml:Issuer><saml:NameID>federated-subject</saml:NameID><samlp:SessionIndex>_session</samlp:SessionIndex></samlp:LogoutRequest>'
    xml=template.format(stamp=stamp(),destination=destination)
    query=signed_query('SAMLRequest',xml,private)
    unsigned=query.split('&Signature=')[0]
    assert client.get(destination+'?'+unsigned).status_code==403
    stale=template.format(stamp=stamp(-600),destination=destination)
    assert client.get(destination+'?'+signed_query('SAMLRequest',stale,private)).status_code==403
    assert app.state.store.one("SELECT user_id FROM sessions WHERE user_id='federated-user'")
    good=client.get(destination+'?'+query,follow_redirects=False)
    assert good.status_code==302,good.text
    assert not app.state.store.one("SELECT user_id FROM sessions WHERE user_id='federated-user'")
    assert client.get(destination+'?'+query,follow_redirects=False).status_code==403
    assert app.state.store.one('SELECT COUNT(*) AS n FROM sessions')['n']>=1


def test_sp_logout_signs_request_and_validates_matching_signed_response(workspace):
    client,app,private,provider,origin=setup(workspace)
    started=client.post(origin+f'/api/v1/identity-providers/{provider}/logout',json={},headers={'X-Pliqra-CSRF':'federated-csrf'})
    assert started.status_code==200,started.text
    query=parse_qs(urlsplit(started.json()['redirect_url']).query)
    assert query.get('Signature')
    relay=query['RelayState'][0]
    flow=app.state.store.one("SELECT * FROM saml_flows WHERE kind='logout'")
    destination=origin+f'/auth/saml/{provider}/sls'
    xml=f'<samlp:LogoutResponse xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_logout_response" Version="2.0" IssueInstant="{stamp()}" Destination="{destination}" InResponseTo="{flow["request_id"]}"><saml:Issuer>https://idp.test</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status></samlp:LogoutResponse>'
    response=client.get(destination+'?'+signed_query('SAMLResponse',xml,private,relay),follow_redirects=False)
    assert response.status_code==302,response.text
    assert not app.state.store.one("SELECT * FROM saml_flows WHERE kind='logout'")
    assert client.get(destination+'?'+signed_query('SAMLResponse',xml,private,relay),follow_redirects=False).status_code==403

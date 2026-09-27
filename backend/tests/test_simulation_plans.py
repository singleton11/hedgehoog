"""Plans are owner-scoped saved simulations, not copies or recalculations."""
from copy import deepcopy

from bson import ObjectId

from test_customer_planner import planner, calculate, save, target, ttl_clock
from test_market_data import api, clock, config, db
from test_portfolio_tracker import portfolio


def test_promote_saved_simulation_idempotently_without_changing_snapshot(planner, db):
    client, customer, goal, url = planner
    preview = calculate(client, url)
    endpoint = f"{url}/simulations/{preview['id']}/plan"
    assert client.post(endpoint).status_code == 404
    assert client.get(f"/customers/{customer['id']}/plans").json()['total'] == 0
    saved = save(client, url, preview)
    before = deepcopy(db.planner_simulations.find_one({'_id': preview['id']}))
    assert not saved['is_plan']
    assert client.post(endpoint, json={'result': {}}).status_code == 422
    first = client.post(endpoint, json={})
    assert first.status_code == 200
    assert first.json() == {**saved, 'is_plan': True}
    assert client.post(endpoint).json() == first.json()
    assert db.planner_simulations.find_one({'_id': preview['id']}) == {**before, 'is_plan': True}
    assert client.get(f"{url}/simulations/{preview['id']}").json()['is_plan'] is True
    assert client.get(url + '/simulations').json()['items'][0]['is_plan'] is True
    plans = client.get(f"/customers/{customer['id']}/plans?limit=1&offset=0").json()
    assert plans['total'] == 1 and plans['items'][0]['id'] == preview['id']
    assert plans['items'][0]['goal_id'] == goal['id']
    assert client.get(f"/customers/{customer['id']}/plans?limit=1&offset=1").json()['items'] == []


def test_assignment_rejects_goals_previews_and_unmarked_saved_simulations(planner, db):
    client, customer, goal, url = planner
    preview = calculate(client, url)
    meta, portfolio_url = portfolio(client, customer['id'])
    base = f"/customers/{customer['id']}/portfolios"
    for invalid in [goal['id'], preview['id'], str(ObjectId())]:
        for method, path in [(client.post, base), (client.patch, portfolio_url)]:
            assert method(path, json={'name': 'Investments', 'plan_id': invalid}).status_code == 404
    save(client, url, preview)
    assert client.patch(portfolio_url, json={'name': 'Investments', 'plan_id': preview['id']}).status_code == 404
    assert client.patch(portfolio_url, json={'name': 'Investments', 'goal_id': goal['id']}).status_code == 422
    client.post(f"{url}/simulations/{preview['id']}/plan")
    assigned = client.patch(portfolio_url, json={'name': 'Investments', 'plan_id': preview['id']})
    assert assigned.status_code == 200 and assigned.json()['plan_id'] == preview['id']
    # Existing goal attachments are retained in storage, but never guessed into a plan.
    db.portfolios.update_one({'_id': meta['id']}, {'$unset': {'plan_id': ''}, '$set': {'goal_id': goal['id']}})
    assert client.get(portfolio_url).json()['portfolio']['plan_id'] is None
    assert db.portfolios.find_one({'_id': meta['id']})['goal_id'] == goal['id']


def test_plan_promotion_and_listing_are_owner_and_customer_scoped(planner, db):
    client, customer, goal, url = planner
    saved = save(client, url, calculate(client, url))
    other_customer, other_goal, other_url = target(client, 'Other customer')
    assert client.post(f"{other_url}/simulations/{saved['id']}/plan").status_code == 404
    endpoint = f"{url}/simulations/{saved['id']}/plan"
    assert client.post(endpoint, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(endpoint).status_code == 200
    assert client.get(f"/customers/{other_customer['id']}/plans").json()['total'] == 0
    other_portfolio, portfolio_url = portfolio(client, other_customer['id'])
    assert client.patch(portfolio_url, json={'name': 'Bad', 'plan_id': saved['id']}).status_code == 404
    # A different owner's marked snapshot must be excluded even under the same customer id.
    foreign = deepcopy(db.planner_simulations.find_one({'_id': saved['id']}))
    foreign.update(_id=str(ObjectId()), user_id='another-owner')
    db.planner_simulations.insert_one(foreign)
    assert client.get(f"/customers/{customer['id']}/plans").json()['total'] == 1
    assert client.post(f"{url}/simulations/{foreign['_id']}/plan").status_code == 404
    _, own_portfolio_url = portfolio(client, customer['id'])
    assert client.patch(own_portfolio_url, json={'name': 'Bad', 'plan_id': foreign['_id']}).status_code == 404
    client.post('/auth/logout')
    assert client.post(endpoint).status_code == 401
    assert client.get(f"/customers/{customer['id']}/plans").status_code == 401

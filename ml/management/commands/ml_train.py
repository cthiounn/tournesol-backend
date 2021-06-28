
from django.core.management.base import BaseCommand, CommandError
from numpy.core.numeric import full

from tournesol.models import Comparison
from settings.settings import VIDEO_FIELDS

import numpy as np
import torch

from ml.management.commands.flower import get_flower
from ml.management.commands.utilities import rescale_rating, sort_by_first, one_hot_vids, get_all_vids, reverse_idxs

"""
Machine Learning main python file

Organisation:
- Data is handled here
- ML model and decentralised structure are in "flower.py"
- some helpful small functions are in "utilities.py"


Notations:
- node = user : contributor
- vid = vID : video, video ID
- rating : rating provided by a contributor between 2 videos, in [0,100] or [-1,1]
- score : score of a video outputted by the algorithm, range?

- idx : index
- l_someting : list of someting
- arr : numpy array
- tens : torch tensor
- dic : dictionnary

Usage:

"""

# global variables
CRITERIAS = [   "reliability", "importance", "engaging", "pedagogy", 
                "layman_friendly", "diversity_inclusion", "backfire_risk", 
                "better_habits", "entertaining_relaxing"]

# CRITERIAS = ["reliability"]
EPOCHS = 1



def fetch_data():
    """
    Fetches the data from the Comparisons model

    Returns:
    - comparison_data: list of [contributor_id: int, video_id_1: int, video_id_2: int, criteria: str, score: float, weight: float]
    """
    comparison_data = [
        [comparison.user_id, comparison.video_1_id, comparison.video_2_id, criteria, getattr(comparison, criteria), getattr(comparison, f"{criteria}_weight")]
        for comparison in Comparison.objects.all() for criteria in VIDEO_FIELDS
        if hasattr(comparison, criteria)]
    return comparison_data

def select_criteria(comparison_data, crit):
    ''' 
    Extracts data for this criteria where score is not None 
    
    Returns: 
    - list of all ratings for this criteria
        (one element is [contributor_id: int, video_id_1: int, video_id_2: int, criteria: str (crit), score: float, weight: float])
    '''
    l_ratings = [comp for comp in comparison_data if (comp[3] == crit and comp[4] is not None)]
    return l_ratings

def shape_data(l_ratings):
    ''' 
    l_ratings : list of not None ratings for one criteria, all users

    Returns : one array with 4 columns : userID, vID1, vID2, rating ([-1,1]) 
    '''
    l_cleared = [rating[:3] + [rescale_rating(rating[4])] for rating in l_ratings]
    arr = np.asarray(l_cleared)
    return arr

def distribute_data(arr, gpu=False): 
    ''' 
    Distributes data on nodes according to user IDs for one criteria

    arr : np 2D array of all ratings for all users for one criteria
            (one line is [userID, vID1, vID2, score])

    Returns:
    - list of (vID1_batch, vID2_batch, rating_batch, single_vIDs_batch) (1/user)
    - list of users IDs in same order
    - dictionnary of {vID: video idx}
    '''
    arr = sort_by_first(arr) # sorting by user IDs
    user_ids , first_of_each = np.unique(arr[:,0], return_index=True)
    first_of_each = list(first_of_each)
    first_of_each.append(len(arr)) # to have last index too
    vids = get_all_vids(arr)
    dic = reverse_idxs(vids)
    data_distrib = []    # futur list of data for each user

    for i in range(len(first_of_each) - 1):
        node_arr = arr[first_of_each[i]: first_of_each[i+1], :]
        l1 = node_arr[:,1]
        l2 = node_arr[:,2]
        batchvids = get_all_vids(node_arr)
        batch1 = one_hot_vids(dic, l1)
        batch2 = one_hot_vids(dic, l2)
        batchout = torch.FloatTensor(node_arr[:,3])
        quadruple = (batch1, batch2, batchout, batchvids)
        data_distrib.append(quadruple)

    return data_distrib, user_ids, dic

#def 

def in_and_out(comparison_data, criteria):
    ''' 
    Trains models and returns video scores

    comparison_data: output of fetch_data()
    criteria: str, rating criteria
    
    Returns :   
    - (tensor of all vIDS , tensor of global video scores)
    - (list of tensor of local vIDs , list of tensors of local video scores)
    - list of users IDs in same order as second output
    '''
    one_crit = select_criteria(comparison_data, criteria)
    full_data = shape_data(one_crit)
    distributed, users_ids, dic = distribute_data(full_data)
    flow = get_flower(len(dic), dic)
    flow.set_allnodes(distributed, users_ids)
    h = flow.train(EPOCHS, verb=2)
    glob, loc = flow.output_scores()
    return glob, loc, users_ids

def format_out_glob(glob, crit):
    ''' 
    Puts data in list of global scores (one criteria)
    
    glob: global scores in 2D tensor (1 line: [vID, score])
    crit: criteria
    
    Returns: 
    - list of [video_id: int, criteria_name: str, score: float, uncertainty: float]
    '''
    l_out = []
    ids, scores = glob
    for i in range(len(ids)):
        out = [int(ids[i]), crit, round(scores[i].item(), 2), 0] # uncertainty is 0 for now
        l_out.append(out)
    return l_out

def format_out_loc(loc, users_ids, crit):
    ''' 
    Puts data in list of local scores (one criteria)

    loc: 
    users_ids: list of user IDs in same order
    
    Returns : 
    - list of [contributor_id: int, video_id: int, criteria_name: str, score: float, uncertainty: float]
    '''
    l_out = []
    vids, scores = loc
    for user_id, user_vids, user_scores in zip(users_ids, vids, scores):
        for i in range(len(user_vids)):
            out = [int(user_id), int(user_vids[i].item()), 
                    crit, round(user_scores[i].item(), 2), 0] # uncertainty is 0 for now
            l_out.append(out)
    return l_out

def ml_run(comparison_data):
    """
    Uses data loaded

    Returns:
    - video_scores: list of [video_id: int, criteria_name: str, score: float, uncertainty: float]
    - contributor_rating_scores: list of [contributor_id: int, video_id: int, criteria_name: str, score: float, uncertainty: float]
    """ # not better to regroup contributors in same list or smthg ?
    video_scores, contributor_rating_scores = [], []
    for crit in CRITERIAS:
        print("\nPROCESSING", crit)
        glob, loc, users_ids = in_and_out(comparison_data, crit) # training
        # putting in required shape for output
        out_glob = format_out_glob(glob, crit) 
        out_loc = format_out_loc(loc, users_ids, crit) 
        video_scores += out_glob
        contributor_rating_scores += out_loc
    return video_scores, contributor_rating_scores

def save_data(video_scores, contributor_rating_scores):
    """
    Saves in the scores for Videos and ContributorRatings
    """
    pass

class Command(BaseCommand):
    help = 'Runs the ml'

    def handle(self, *args, **options):
        comparison_data = fetch_data()
        global_scores, contributor_scores = ml_run(comparison_data)
        save_data(global_scores, contributor_scores)

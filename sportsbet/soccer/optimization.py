"""
Includes classes and functions to test and select the optimal 
betting strategy on historical and current data.
"""

# Author: Georgios Douzas <gdouzas@icloud.com>
# License: BSD 3 clause

from pathlib import Path
from os.path import join

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin, clone
from sklearn.utils import check_array, check_X_y
from sklearn.model_selection._split import BaseCrossValidator, _num_samples
from sklearn.metrics import precision_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MinMaxScaler
from sklearn.dummy import DummyClassifier
from imblearn.pipeline import Pipeline
import progressbar

from .. import PATH, ProfitEstimator, total_profit_score
from .data import _fetch_spi_data, _fetch_fd_data, _match_teams_names, LEAGUES_MAPPING

DATA_PATH = join(PATH, 'training_data.csv')


class SeasonTimeSeriesSplit(BaseCrossValidator):
    """Season time series cross-validator.
    Parameters
    ----------
    test_season : str, default='17-18'
        The testing season.
    max_day_range: int
        The maximum day range of each test fold.
    """

    def __init__(self, test_year=2, max_day_range=6):
        self.test_year = test_year
        self.max_day_range = max_day_range

    def _generate_season_indices(self, X):
        """Generate season indices to use in test set."""

        # Check input array
        X = check_array(X, dtype=None)
        
        # Define days
        self.days_ = X[:, 0]

        # Define all and season indices
        indices = np.arange(_num_samples(X))
        start_day, end_day = 365 * (self.test_year - 1), 365 * self.test_year
        season_indices = indices[(self.days_ >= start_day) & (self.days_ < end_day)]

        return season_indices


    def split(self, X, y=None, groups=None):
        """Generates indices to split data into training and test set.
        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.
        y : array-like, shape (n_samples,)
            Always ignored, exists for compatibility.
        groups : array-like, with shape (n_samples,), optional
            Always ignored, exists for compatibility.
        Returns
        -------
        train_indices : ndarray
            The training set indices for that split.
        test_indices : ndarray
            The testing set indices for that split.
        """

        # Generate season indices
        season_indices = self._generate_season_indices(X)

        # Yield train and test indices
        start_ind = season_indices[0]
        for ind in season_indices:
            if self.days_[ind] - self.days_[start_ind] >= self.max_day_range:
                train_indices = np.arange(0, start_ind)
                test_indices = np.arange(start_ind, ind)
                start_ind = ind
                yield (train_indices, test_indices)

    def get_n_splits(self, X, y=None, groups=None):
        """Returns the number of splitting iterations in the cross-validator

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.

        y : object
            Always ignored, exists for compatibility.

        groups : object
            Always ignored, exists for compatibility.

        Returns
        -------
        n_splits : int
            Returns the number of splitting iterations in the cross-validator.
        """

        # Generate season indices
        season_indices = self._generate_season_indices(X)

        # Calculate number of splits
        start_ind, n_splits = season_indices[0], 0
        for ind in season_indices:
            if self.days_[ind] - self.days_[start_ind] >= self.max_day_range:
                n_splits += 1
                start_ind = ind
        
        return n_splits


class BettingAgent:

    def fetch_training_data(self, leagues='all'):
        """Fetch the training data."""

        # Validate input
        valid_leagues = [league_id for league_id, _ in LEAGUES_MAPPING.values()]
        if leagues not in ('all', 'main') and not set(leagues).issubset(valid_leagues):
            msg = "The `leagues` parameter should be either equal to 'all' or 'main' or a list of valid league ids. Got {} instead."
            raise ValueError(msg.format(leagues))

        # Define parameters 
        avg_odds_features = ['HomeAverageOdd', 'AwayAverageOdd', 'DrawAverageOdd']
        keys = ['Date', 'League', 'HomeTeam', 'AwayTeam', 'HomeGoals', 'AwayGoals']

        # Fetch data
        spi_data = _fetch_spi_data(leagues)
        fd_data = _fetch_fd_data(leagues)

        # Teams names matching
        mapping = _match_teams_names(spi_data, fd_data)
        spi_data['HomeTeam'] = spi_data['HomeTeam'].apply(lambda team: mapping[team])
        spi_data['AwayTeam'] = spi_data['AwayTeam'].apply(lambda team: mapping[team])

        # Probabilities data
        probs = 1 / fd_data.loc[:, avg_odds_features].values
        probs = pd.DataFrame(probs / probs.sum(axis=1)[:, None], columns=['HomeFDProb', 'AwayFDProb', 'DrawFDProb'])
        probs_data = pd.concat([probs, fd_data], axis=1)
        probs_data.drop(columns=avg_odds_features, inplace=True)

        # Combine data
        training_data = pd.merge(spi_data, probs_data, on=keys)

        # Create features
        training_data['DiffSPIGoals'] = training_data['HomeSPIGoals'] - training_data['AwaySPIGoals']
        training_data['DiffSPI'] = training_data['HomeSPI'] - training_data['AwaySPI']
        training_data['DiffSPIProb'] = training_data['HomeSPIProb'] - training_data['AwaySPIProb']
        training_data['DiffFDProb'] = training_data['HomeFDProb'] - training_data['AwayFDProb']

        # Create day index
        training_data['Day'] = (training_data.Date - min(training_data.Date)).dt.days

        # Sort data
        training_data = training_data.sort_values(keys[:-2]).reset_index(drop=True)

        # Drop features
        training_data.drop(columns=['Date', 'Season', 'League', 'HomeTeam', 'AwayTeam'], inplace=True)

        # Save data
        Path(PATH).mkdir(exist_ok=True)
        training_data.to_csv(DATA_PATH, index=False) 

    def load_modeling_data(self, predicted_result='A'):
        """Load the data used for modeling."""

        # Load data
        try:
            training_data = pd.read_csv(DATA_PATH)
        except FileNotFoundError:
            raise FileNotFoundError('Training data do not exist. Fetch training data before loading modeling data.')

        # Split and prepare data
        X = training_data.drop(columns=['HomeMaximumOdd', 'AwayMaximumOdd', 'DrawMaximumOdd', 'HomeGoals', 'AwayGoals'])
        X = X[['Day'] + X.columns[:-1].tolist()]
        y = (training_data['HomeGoals'] - training_data['AwayGoals']).apply(lambda sign: 'H' if sign > 0 else 'D' if sign == 0 else 'A')
        y = (y == predicted_result).astype(int)
        odds = training_data.loc[:, {'H': 'HomeMaximumOdd', 'A': 'AwayMaximumOdd', 'D': 'DrawMaximumOdd'}[predicted_result]] 

        # Check arrays
        X, y = check_X_y(X, y, dtype=None)
        odds = check_array(odds, dtype=None, ensure_2d=False)

        # Normalize array
        X = np.hstack([X[:, 0].reshape(-1, 1), MinMaxScaler().fit_transform(X[:, 1: ])])

        return X, y, odds

    def backtest(self, estimator=None, fit_params=None, predicted_result='A', test_year=2, max_day_range=6):
        """Apply backtesting to betting agent."""

        # Load backtesting_data
        X, y, odds = self.load_modeling_data(predicted_result)

        # Prepare data
        X = np.hstack((X, odds.reshape(-1, 1)))

        # Define parameters
        self.estimator_ = ProfitEstimator(estimator) if estimator is not None else ProfitEstimator(DummyClassifier(strategy='constant', constant=1))
        self.fit_params_ = {} if fit_params is None else fit_params.copy()
        validation_size = self.fit_params_.pop('validation_size') if 'validation_size' in self.fit_params_ else None

        # Define train and test indices
        indices = list(SeasonTimeSeriesSplit(test_year=test_year, max_day_range=max_day_range).split(X, y))

        # Define progress bar
        bar = progressbar.ProgressBar(min_value=1, max_value=len(indices))
        
        # Define results placeholder
        self.backtest_results_ = []

        # Run cross-validation
        for ind, (train_indices, test_indices) in enumerate(indices):
            
            # Split to train and test data
            X_train, X_test, y_train, y_test = X[train_indices, 1:], X[test_indices, 1:], y[train_indices], y[test_indices]

            # Append validation data
            if validation_size is not None:
                
                # Split to train and validation data
                X_train, X_val, y_train, y_val = train_test_split(X_train, y_train, test_size=validation_size, shuffle=False)
                
                # Update fitting parameters
                self.fit_params_['xgbclassifier__eval_set'] = [(X_val[:, :-1], y_val)]

            # Get test predictions
            y_pred = self.estimator_.fit(X_train, y_train, **self.fit_params_).predict(X_test)

            # Append results
            self.backtest_results_.append((y_test, y_pred))

            # Update progress bar
            bar.update(ind + 1)

    def calculate_backtest_stats(self, bet_factor=1.5, credit_exponent=3):

        # Initialize parameters
        statistics, precisions = [], []
        capital, bet_amount = 1.0, 1.0
        y_test_all, y_pred_all = np.array([]), np.array([])
        

        for y_test, y_pred in self.backtest_results_:

            # Append results and predictions
            y_test_all, y_pred_all = np.hstack((y_test, y_test_all)), np.hstack((y_pred, y_pred_all))
            
            # Convert to binary
            y_pred_bin = (y_pred > 0).astype(int)

            # Calculate number of bets and matches
            n_bets = y_pred_bin.sum()
            n_matches = y_pred.size

            # Calculate precision
            precision = precision_score(y_test, y_pred_bin) if n_bets > 0 else np.nan
            precisions.append(precision)

            # Calculate profit
            profit = bet_amount * total_profit_score(y_test, y_pred) / n_bets if n_bets > 0 else 0.0
            
            # Calculate capital
            capital += profit

            # Adjust bet amount
            bet_amount = bet_amount * bet_factor if profit < 0.0 else 1.0

            # Calculate credit
            max_credit = capital + bet_factor ** credit_exponent
            if bet_amount > max_credit:
                bet_amount = max_credit
                
            # Generate statistic
            statistic = (capital, profit, bet_amount, n_bets, n_matches, precision)

            # Append statistic
            statistics.append(statistic)

            if bet_amount == 0:
                break

        # Define statistics dataframe
        statistics = pd.DataFrame(statistics, columns=['Capital', 'Profit', 'Bet amount', 'Bets', 'Matches', 'Precision'])

        # Define attributes
        self.profit_per_bet_ = total_profit_score(y_test_all, y_pred_all) / y_pred_all.size
        self.precision_ = np.nanmean(precisions)

        return statistics
